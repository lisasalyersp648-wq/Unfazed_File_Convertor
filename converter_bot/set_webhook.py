"""Register or inspect the Telegram webhook.

Usage (from the ``converter_bot`` directory):

    python set_webhook.py --info
    python set_webhook.py --url https://your-app.vercel.app/api/webhook
    python set_webhook.py --base-url https://your-app.vercel.app
    python set_webhook.py --delete

``WEBHOOK_URL`` and ``WEBHOOK_SECRET`` from the environment are used when the
corresponding arguments are omitted.  Vercel production deployments set the
webhook automatically, but running this once is the most reliable way to wire
up the first deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from telegram import Bot

from config import ALLOWED_UPDATES, BOT_TOKEN, WEBHOOK_PATH, WEBHOOK_SECRET, WEBHOOK_URL


def _resolve_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url.strip().rstrip("/")
    if args.base_url:
        return args.base_url.strip().rstrip("/") + WEBHOOK_PATH
    if WEBHOOK_URL:
        return WEBHOOK_URL
    raise SystemExit(
        "No webhook URL. Pass --url, --base-url, or set WEBHOOK_URL in the "
        "environment."
    )


def _format_timestamp(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


async def _run(args: argparse.Namespace) -> int:
    bot = Bot(BOT_TOKEN)

    try:
        await bot.initialize()

        if args.info:
            info = await bot.get_webhook_info()
            print("Webhook status")
            print(f"  url:                 {info.url or '(not set)'}")
            print(f"  pending updates:     {info.pending_update_count}")
            print(f"  max connections:     {info.max_connections}")
            print(f"  allowed updates:     {info.allowed_updates or '(default)'}")
            print(f"  custom certificate:  {info.has_custom_certificate}")
            print(f"  last error date:     {_format_timestamp(info.last_error_date)}")
            print(f"  last error message:  {info.last_error_message or '-'}")
            return 0

        if args.delete:
            await bot.delete_webhook(drop_pending_updates=args.drop_pending)
            print("Webhook deleted.")
            return 0

        url = _resolve_url(args)
        secret = (args.secret or WEBHOOK_SECRET or "").strip()

        if not secret:
            raise SystemExit(
                "A webhook secret is required. Pass --secret or set "
                "WEBHOOK_SECRET. Generate one with: python -c \"import "
                "secrets; print(secrets.token_hex(32))\""
            )
        if len(secret) < 16:
            raise SystemExit("The webhook secret must be at least 16 characters.")
        if not all(ch.isalnum() or ch in "_-" for ch in secret):
            raise SystemExit(
                "The webhook secret may only contain letters, digits, "
                "underscores, and hyphens."
            )

        await bot.set_webhook(
            url=url,
            secret_token=secret,
            allowed_updates=ALLOWED_UPDATES,
            max_connections=args.max_connections,
            drop_pending_updates=args.drop_pending,
        )
        print(f"Webhook registered at {url}")
        return 0
    finally:
        await bot.shutdown()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the Telegram webhook for the file converter bot."
    )
    parser.add_argument("--url", help="Full webhook URL, including the path")
    parser.add_argument(
        "--base-url",
        help=f"Public base URL; {WEBHOOK_PATH} is appended automatically",
    )
    parser.add_argument("--secret", help="Webhook secret (or WEBHOOK_SECRET)")
    parser.add_argument(
        "--max-connections",
        type=int,
        default=40,
        help="Maximum concurrent Telegram connections (default: 40)",
    )
    parser.add_argument(
        "--drop-pending",
        action="store_true",
        help="Drop updates that were queued before registration",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--info", action="store_true", help="Show webhook status")
    mode.add_argument("--delete", action="store_true", help="Remove the webhook")

    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
