"""Incoming file handler: show the available target formats."""

from __future__ import annotations

import html
import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
from config import ENABLE_MEDIA
from converters import available_targets
from utils import CATEGORY_MAP, MIME_MAP, get_category, get_limit_mb

logger = logging.getLogger(__name__)

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

CATEGORY_EMOJI = {
    "document": "\U0001f4c4",
    "image": "\U0001f5bc\ufe0f",
    "audio": "\U0001f3b5",
    "video": "\U0001f3ac",
}

SEP = "\u2501" * 27

# Telegram caps callback_data at 64 bytes. "convert:<src>:<tgt>:<token>" with a
# 12-char token stays well under that.
_CALLBACK_PREFIX = "convert:"


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _normalise_mime(mime_type: str | None) -> str:
    if not mime_type:
        return ""
    return mime_type.split(";", 1)[0].strip().lower()


async def _build_keyboard(
    targets: list[str],
    source_format: str,
    file_id: str,
    original_name: str,
    file_size: int,
    user_id: int,
) -> InlineKeyboardMarkup:
    # Store only a short token in the button. The file metadata lives in the
    # database so the callback works on a different serverless instance.
    token = secrets.token_urlsafe(9)
    await db.save_pending_file(
        token=token,
        user_id=user_id,
        file_id=file_id,
        file_name=original_name,
        file_size=file_size,
    )

    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for target in targets:
        emoji = FORMAT_EMOJI.get(target, "")
        label = f"{emoji} {target.upper()}" if emoji else target.upper()
        row.append(
            InlineKeyboardButton(
                label,
                callback_data=f"{_CALLBACK_PREFIX}{source_format}:{target}:{token}",
            )
        )
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    file_obj = None
    file_name = None
    mime_type = None
    file_size = 0

    if msg.document:
        file_obj = msg.document
        file_name = file_obj.file_name or "document"
        mime_type = file_obj.mime_type
        file_size = file_obj.file_size or 0
    elif msg.photo:
        file_obj = msg.photo[-1]
        file_name = "photo.jpg"
        mime_type = "image/jpeg"
        file_size = file_obj.file_size or 0
    elif msg.audio:
        file_obj = msg.audio
        mime_type = _normalise_mime(file_obj.mime_type)
        ext = MIME_MAP.get(mime_type, "mp3")
        file_name = file_obj.file_name or f"audio.{ext}"
        file_size = file_obj.file_size or 0
    elif msg.voice:
        file_obj = msg.voice
        mime_type = _normalise_mime(file_obj.mime_type) or "audio/ogg"
        file_name = "voice.ogg"
        file_size = file_obj.file_size or 0
    elif msg.video:
        file_obj = msg.video
        mime_type = _normalise_mime(file_obj.mime_type) or "video/mp4"
        ext = MIME_MAP.get(mime_type, "mp4")
        file_name = file_obj.file_name or f"video.{ext}"
        file_size = file_obj.file_size or 0
    else:
        await msg.reply_text("Please send a document, image, audio, or video file.")
        return

    if not file_obj:
        return

    await send_format_options(
        msg,
        context,
        file_obj,
        file_name,
        mime_type,
        file_size,
        user_id=update.effective_user.id,
    )


async def send_format_options(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    file_obj,
    file_name: str,
    mime_type: str | None,
    file_size: int,
    user_id: int,
) -> None:
    """Show the target-format keyboard for a file object.

    Shared by direct uploads and the Library Bot deep-link bridge.
    """
    mime_type = _normalise_mime(mime_type)
    source_format = _detect_from_meta(mime_type, file_name)
    if not source_format:
        ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "unknown"
        await message.reply_text(
            f"Sorry, I don't support <code>.{html.escape(ext)}</code> files yet.",
            parse_mode="HTML",
        )
        return

    targets = available_targets(source_format)
    if not targets:
        category = get_category(source_format)
        if category in ("audio", "video"):
            await message.reply_text(
                "\U0001f507 <b>Audio/video conversion is not available</b>\n"
                "This deployment runs without ffmpeg. Documents and images are "
                "still supported.",
                parse_mode="HTML",
            )
        else:
            ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "unknown"
            await message.reply_text(
                f"Sorry, I don't support <code>.{html.escape(ext)}</code> files yet.",
                parse_mode="HTML",
            )
        return

    limit_mb = get_limit_mb(source_format)
    category = get_category(source_format)
    cat_emoji = CATEGORY_EMOJI.get(category, "\U0001f4c1")

    size_str = _format_bytes(file_size) if file_size else ""
    escaped_name = html.escape(file_name)

    keyboard = await _build_keyboard(
        targets,
        source_format,
        file_obj.file_id,
        file_name,
        file_size,
        user_id,
    )

    lines = [
        f"{cat_emoji} <b>{escaped_name}</b>",
        SEP,
    ]
    info_parts = []
    if size_str:
        info_parts.append(f"Size: <code>{size_str}</code>")
    if limit_mb:
        info_parts.append(f"Max: <code>{limit_mb} MB</code>")
    if info_parts:
        lines.append(" \u00b7 ".join(info_parts))
        lines.append(SEP)
    if not ENABLE_MEDIA and category in ("audio", "video"):
        lines.append("Audio/video conversion is disabled on this deployment.")
        lines.append(SEP)
    lines.append("")
    lines.append("<b>Select target format:</b>")

    await message.reply_text(
        "\n".join(lines),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


def _detect_from_meta(mime_type: str, file_name: str) -> str | None:
    mime_type = _normalise_mime(mime_type)
    if mime_type and mime_type in MIME_MAP:
        return MIME_MAP[mime_type]
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext in CATEGORY_MAP:
        return ext
    return None
