"""Vercel-compatible ASGI webhook.

Telegram delivers updates over HTTPS.  Vercel may freeze or recycle the
function as soon as the HTTP response is sent, so every update is processed
*before* a ``200`` is returned.  Expensive work is bounded by
``CONVERSION_TIMEOUT`` and the function's ``maxDuration`` in ``vercel.json``.

The application is initialised lazily because Vercel does not guarantee a
lifespan event.  It is cached per event loop: the serverless runtime reuses
both the process and the loop between invocations, but a rebuilt loop (tests,
local reloads) must get a fresh ``Application`` because httpx clients are
bound to the loop that created them.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable

from telegram import Update

import db
from config import (
    ALLOWED_UPDATES,
    AUTO_SET_WEBHOOK,
    ENABLE_MEDIA,
    IS_VERCEL,
    LOG_LEVEL,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=getattr(logging, LOG_LEVEL, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_UPDATE_BYTES = 1024 * 1024

SECRET_HEADER = b"x-telegram-bot-api-secret-token"


class BadRequestError(Exception):
    """The request was not a usable Telegram update."""


# ---------------------------------------------------------------------------
# Lazily initialised Application
# ---------------------------------------------------------------------------

_application: Any = None
_application_loop: asyncio.AbstractEventLoop | None = None
_application_lock: asyncio.Lock | None = None
_application_lock_loop: asyncio.AbstractEventLoop | None = None
_webhook_attempted = False


async def _ensure_webhook(application: Any) -> None:
    """Register the webhook once per process when auto-registration is on."""
    global _webhook_attempted

    if _webhook_attempted or not AUTO_SET_WEBHOOK or not WEBHOOK_URL:
        return

    _webhook_attempted = True
    try:
        await asyncio.wait_for(
            application.bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET or None,
                allowed_updates=ALLOWED_UPDATES,
                max_connections=40,
                drop_pending_updates=False,
            ),
            timeout=15,
        )
        logger.info("Webhook registered at %s", WEBHOOK_URL)
    except Exception:
        # Allow a later health check or webhook delivery to retry.
        _webhook_attempted = False
        logger.exception("Could not register the webhook at %s", WEBHOOK_URL)


async def get_application() -> Any:
    """Return an initialised ``Application`` bound to the current loop."""
    global _application, _application_loop
    global _application_lock, _application_lock_loop
    global _webhook_attempted

    loop = asyncio.get_running_loop()
    if _application is not None and _application_loop is loop:
        return _application

    if _application_lock is None or _application_lock_loop is not loop:
        # A lock (and the Application it guards) is bound to the loop that
        # awaits it.  Until this assignment runs, ``_application_loop`` is
        # still None, so it cannot be used to decide whether the lock is
        # reusable: two concurrent cold-start requests would each replace the
        # lock and initialise a second Application.
        _application_lock = asyncio.Lock()
        _application_lock_loop = loop

    async with _application_lock:
        if _application is not None and _application_loop is loop:
            return _application

        # Imported here so /health stays cheap and does not need PyMuPDF.
        from bot import build_application

        application = build_application(webhook=True)
        try:
            await application.initialize()
            # PTB only runs these hooks from run_polling()/run_webhook(); the
            # ASGI entrypoint initialises the Application directly.
            if application.post_init:
                await application.post_init(application)
        except Exception:
            try:
                await application.shutdown()
                if application.post_shutdown:
                    await application.post_shutdown(application)
            except Exception:
                pass
            raise

        _application = application
        _application_loop = loop
        _webhook_attempted = False
        await _ensure_webhook(application)
        return application


async def _shutdown_application() -> None:
    global _application, _application_loop
    global _application_lock, _application_lock_loop
    global _webhook_attempted

    application = _application
    application_loop = _application_loop
    loop = asyncio.get_running_loop()

    _application = None
    _application_loop = None
    _application_lock = None
    _application_lock_loop = None
    _webhook_attempted = False

    if application is None or application_loop is not loop:
        return
    try:
        await application.shutdown()
        if application.post_shutdown:
            await application.post_shutdown(application)
    except Exception:
        logger.exception("Application shutdown failed")


async def _warm_up() -> None:
    """Initialise the bot (and register the webhook when configured)."""
    application = await get_application()
    # ``get_application`` only registers on creation.  If that attempt failed,
    # a later /health request should be able to retry it.
    await _ensure_webhook(application)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _normalise_path(path: str) -> str:
    path = (path or "/").split("?", 1)[0]
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path


_WEBHOOK_ROUTE = _normalise_path(WEBHOOK_PATH)


def _header(scope: dict, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


async def _read_body(receive: Callable[[], Awaitable[dict]]) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            raise BadRequestError("Client disconnected before sending the body")
        if message_type != "http.request":
            continue
        body.extend(message.get("body", b""))
        if len(body) > MAX_UPDATE_BYTES:
            raise BadRequestError("Update is too large")
        if not message.get("more_body", False):
            return bytes(body)


async def _send_json(
    send: Callable[[dict], Awaitable[None]],
    status: int,
    payload: dict,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    body_allowed: bool = True,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send(
        {
            "type": "http.response.body",
            "body": body if body_allowed else b"",
            "more_body": False,
        }
    )


async def _authorise(scope: dict) -> bool:
    if not WEBHOOK_SECRET:
        # Local development may run without a secret.  On Vercel every request
        # must be authenticated, including preview deployments that do not
        # derive a production webhook URL.
        return not IS_VERCEL
    provided = _header(scope, SECRET_HEADER)
    return bool(provided) and hmac.compare_digest(provided, WEBHOOK_SECRET)


# ---------------------------------------------------------------------------
# Update processing
# ---------------------------------------------------------------------------


async def _process_update(body: bytes) -> None:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadRequestError("Body is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise BadRequestError("Update must be a JSON object")
    if "update_id" not in payload:
        raise BadRequestError("Update is missing update_id")

    application = await get_application()
    update = Update.de_json(payload, application.bot)
    if update is None:
        logger.warning("Ignoring an empty Telegram update")
        return

    # Runs handlers (including error handlers) to completion.  In webhook mode
    # the Application is built with ``concurrent_updates=False`` so a single
    # invocation does not interleave updates.
    await application.process_update(update)


async def _health(send, *, body_allowed: bool) -> None:
    warmed = _application is not None

    # A deployment can be the first thing to ever configure the webhook, so a
    # simple health check doubles as a warm-up/registration hook.
    if AUTO_SET_WEBHOOK and not _webhook_attempted:
        try:
            await _warm_up()
            warmed = True
        except Exception:
            logger.exception("Health warm-up failed")

    await _send_json(
        send,
        200,
        {
            "ok": True,
            "service": "file-converter-bot",
            "database": db.backend_name(),
            "media": bool(ENABLE_MEDIA),
            "webhook_configured": bool(WEBHOOK_URL),
            "webhook_path": WEBHOOK_PATH,
            "initialized": warmed,
            "time": int(time.time()),
        },
        body_allowed=body_allowed,
    )


async def _handle_http(scope: dict, receive, send) -> None:
    method = (scope.get("method") or "GET").upper()
    path = _normalise_path(scope.get("path") or "/")
    body_allowed = method != "HEAD"

    if path == _WEBHOOK_ROUTE:
        if method != "POST":
            await _send_json(
                send,
                405,
                {"ok": False, "error": "Method not allowed"},
                extra_headers=[(b"allow", b"POST")],
                body_allowed=body_allowed,
            )
            return

        if not await _authorise(scope):
            logger.warning("Rejected webhook request with a bad secret token")
            await _send_json(
                send,
                401,
                {"ok": False, "error": "Unauthorized"},
                body_allowed=body_allowed,
            )
            return

        try:
            body = await _read_body(receive)
            await _process_update(body)
        except BadRequestError as exc:
            logger.warning("Rejected webhook request: %s", exc)
            await _send_json(
                send,
                400,
                {"ok": False, "error": str(exc)},
                body_allowed=body_allowed,
            )
            return
        except Exception:
            # A 5xx makes Telegram retry the update, which is what we want for
            # transient database/network failures.
            logger.exception("Failed to process a Telegram update")
            await _send_json(
                send,
                500,
                {"ok": False, "error": "Update processing failed"},
                body_allowed=body_allowed,
            )
            return

        await _send_json(send, 200, {"ok": True}, body_allowed=body_allowed)
        return

    if method in ("GET", "HEAD") and path in ("/", "/health", "/healthz"):
        await _health(send, body_allowed=body_allowed)
        return

    await _send_json(
        send,
        404,
        {"ok": False, "error": "Not found"},
        body_allowed=body_allowed,
    )


async def _handle_lifespan(receive, send) -> None:
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "lifespan.startup":
            try:
                await _warm_up()
            except Exception:
                # Do not poison the process for its whole lifetime: a failed
                # cold start (Telegram or the database briefly unavailable)
                # should be retried by the next request, which initialises
                # lazily.  The health payload reports ``initialized: false``.
                logger.exception(
                    "ASGI startup warm-up failed; requests will retry lazily"
                )
            await send({"type": "lifespan.startup.complete"})
        elif message_type == "lifespan.shutdown":
            await _shutdown_application()
            await send({"type": "lifespan.shutdown.complete"})
            return


async def app(scope: dict, receive, send) -> None:
    """ASGI entrypoint exported by the root ``app.py``."""
    scope_type = scope.get("type")

    if scope_type == "lifespan":
        await _handle_lifespan(receive, send)
        return

    if scope_type != "http":
        await _send_json(
            send,
            404,
            {"ok": False, "error": "Unsupported protocol"},
        )
        return

    try:
        await _handle_http(scope, receive, send)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unhandled ASGI error")
        try:
            await _send_json(
                send,
                500,
                {"ok": False, "error": "Internal server error"},
                body_allowed=(scope.get("method") or "GET").upper() != "HEAD",
            )
        except Exception:
            pass
