"""``/history`` command."""

from __future__ import annotations

import html
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

import db

FORMAT_EMOJI = {
    "pdf": "\U0001f4d5", "docx": "\U0001f4d8", "epub": "\U0001f4d6",
    "txt": "\U0001f4c4", "html": "\U0001f310", "md": "\U0001f4dd",
    "odt": "\U0001f4d3", "rtf": "\U0001f4c3",
    "mobi": "\U0001f4d6", "fb2": "\U0001f4da",
    "jpg": "\U0001f5bc", "jpeg": "\U0001f5bc", "png": "\U0001f5bc",
    "webp": "\U0001f5bc", "gif": "\U0001f5bc", "bmp": "\U0001f5bc",
    "tiff": "\U0001f5bc",
    "mp3": "\U0001f3b5", "ogg": "\U0001f3b5", "wav": "\U0001f3b5",
    "flac": "\U0001f3b5", "m4a": "\U0001f3b5", "opus": "\U0001f3b5",
    "mp4": "\U0001f3ac", "mkv": "\U0001f3ac", "avi": "\U0001f3ac",
    "mov": "\U0001f3ac", "webm": "\U0001f3ac",
}

SEP = "\u2501" * 27


def _format_ts(ts) -> str:
    if not ts:
        return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    text = str(ts)
    if "." in text:
        text = text.split(".")[0]
    return text


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    jobs = await db.get_history(user_id, 10)

    if not jobs:
        await update.message.reply_text(
            "No conversion history yet.\n"
            "Send me a file to get started! \U0001f4e4",
        )
        return

    total = len(jobs)
    success_count = sum(1 for job in jobs if job["status"] == "success")
    lines = [
        f"\U0001f4cb <b>Conversion History</b> <i>(last {total})</i>",
        SEP,
        f"\u2705 {success_count} succeeded  \u00b7  "
        f"\u274c {total - success_count} failed",
        SEP,
        "",
    ]

    for job in jobs:
        src = (job.get("source_format") or "").lower()
        tgt = (job.get("target_format") or "").lower()
        icon = "\u2705" if job["status"] == "success" else "\u274c"
        timestamp = _format_ts(job.get("created_at"))
        filename = html.escape(job.get("original_filename") or "unknown")
        fmt = f"{FORMAT_EMOJI.get(src, '')} {src.upper()} \u2192 "
        fmt += f"{FORMAT_EMOJI.get(tgt, '')} {tgt.upper()}"
        if timestamp:
            fmt += f"  \u2022  {timestamp}"
        lines.append(f"{icon} {fmt}")
        lines.append(f"   \U0001f4c4 <code>{filename}</code>")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
