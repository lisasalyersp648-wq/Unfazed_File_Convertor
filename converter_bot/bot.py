"""Telegram application wiring.

The same handlers are used by:
  * the ASGI webhook entrypoint (Vercel) in ``webhook.py``
  * the long-polling runner below (local development / containers)
  * the standalone webhook runner below (containers behind a public URL)
"""

from __future__ import annotations

import asyncio
import logging
import os
from importlib.metadata import PackageNotFoundError, version

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import (
    ALLOWED_UPDATES,
    BOT_TOKEN,
    LOG_LEVEL,
    LOCAL_API_MODE,
    LOCAL_API_URL,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)
from handlers.channel_handler import channel_member_handler
from handlers.convert_handler import handle_conversion
from handlers.file_handler import handle_file
from handlers.history_handler import history_command
from handlers.start_handler import help_command, start_command

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Show supported formats"),
    BotCommand("history", "Your last 10 conversions"),
    BotCommand("help", "How to use this bot"),
]


def _log_engine_status() -> None:
    logger.info("Database backend: %s", db.backend_name())

    packages = (
        ("python-telegram-bot", "python-telegram-bot"),
        ("pymupdf", "PyMuPDF"),
        ("python-docx", "python-docx"),
        ("mammoth", "mammoth"),
        ("Pillow", "Pillow"),
    )
    for package, label in packages:
        try:
            logger.info("%s %s \u2713", label, version(package))
        except PackageNotFoundError:
            logger.warning("%s is not installed", label)

    if LOCAL_API_URL:
        logger.info("Using local Bot API server at %s", LOCAL_API_URL)
    else:
        logger.info("Using Telegram's cloud Bot API (20 MB download limit)")

    from config import ENABLE_MEDIA

    logger.info("Audio/video conversions: %s", "enabled" if ENABLE_MEDIA else "disabled")


async def on_startup(app: Application) -> None:
    await db.init()
    _log_engine_status()

    try:
        # Never let an API hiccup stall the whole serverless cold start.
        await asyncio.wait_for(app.bot.set_my_commands(BOT_COMMANDS), timeout=10)
    except Exception as exc:
        logger.warning("Could not register bot commands: %s", exc)


async def on_shutdown(app: Application) -> None:
    await db.close()
    logger.info("Shutdown complete")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Unhandled exception while processing an update",
        exc_info=context.error,
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong. Please try again."
            )
        except Exception:
            pass


def _register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.PHOTO
            | filters.AUDIO
            | filters.VOICE
            | filters.VIDEO,
            handle_file,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_conversion, pattern=r"^convert:"))
    app.add_handler(
        ChatMemberHandler(
            channel_member_handler,
            chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )
    app.add_error_handler(error_handler)


def build_application(*, webhook: bool = False) -> Application:
    builder = Application.builder().token(BOT_TOKEN)

    if webhook:
        # No long-polling updater in serverless mode.
        builder.updater(None)

    if LOCAL_API_URL and not webhook:
        builder.base_url(f"{LOCAL_API_URL}/bot")
        builder.base_file_url(f"{LOCAL_API_URL}/file/bot")
        if LOCAL_API_MODE:
            builder.local_mode(True)

    app = (
        builder.read_timeout(120)
        .write_timeout(300)
        .connect_timeout(30)
        .pool_timeout(30)
        # In a serverless webhook the HTTP response must not be sent before the
        # handler finishes, so updates are processed sequentially there.
        .concurrent_updates(not webhook)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    _register_handlers(app)
    return app


def main() -> None:
    run_mode = (os.getenv("RUN_MODE") or "").strip().lower()
    use_webhook = run_mode == "webhook" or (not run_mode and bool(WEBHOOK_URL))

    if use_webhook:
        if not WEBHOOK_URL:
            raise SystemExit(
                "RUN_MODE=webhook requires WEBHOOK_URL to be set."
            )
        port = int(os.getenv("PORT", "8443"))
        url_path = WEBHOOK_PATH.strip("/")
        logger.info("Starting webhook server on port %s (path /%s)", port, url_path)
        # run_webhook() drives an Updater, so this mode keeps the updater. The
        # updater-less application is only used by the ASGI entrypoint.
        build_application(webhook=False).run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=ALLOWED_UPDATES,
            drop_pending_updates=os.getenv("DROP_PENDING_UPDATES", "").lower()
            in ("1", "true", "yes"),
        )
        return

    logger.info("Starting long polling...")
    build_application(webhook=False).run_polling(
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=os.getenv("DROP_PENDING_UPDATES", "").lower()
        in ("1", "true", "yes"),
    )


if __name__ == "__main__":
    main()
