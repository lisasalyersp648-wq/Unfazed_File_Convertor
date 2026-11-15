"""``/start`` and ``/help`` plus the Library Bot deep-link bridge."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import traceback

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from config import DEEPLINK_SECRET, ENABLE_MEDIA, STORAGE_CHANNEL_ID
from handlers.file_handler import send_format_options

logger = logging.getLogger(__name__)

SEP = "\u2501" * 27

_BASE_FORMATS = (
    "\U0001f4c4 <b>Documents:</b> PDF, DOCX, EPUB, TXT, HTML, MD\n\n"
    "\U0001f5bc\ufe0f <b>Images:</b> JPG, PNG, WebP, BMP, GIF, TIFF\n\n"
)

_MEDIA_FORMATS = (
    "\U0001f3b5 <b>Audio:</b> MP3, OGG, WAV, FLAC, M4A, Opus\n\n"
    "\U0001f3ac <b>Video:</b> MP4, MKV, AVI, MOV, WebM\n\n"
)

WELCOME_TEXT = (
    "\U0001f4c1 <b>File Converter Bot</b>\n"
    "Turn your files into any format you need.\n\n"
    f"{SEP}\n"
    "<b>\U0001f4e4 How to use</b>\n"
    "Just send me any file and I'll show you\n"
    "all the formats you can convert it to.\n\n"
    "<b>\U0001f4e6 Supported formats</b>\n"
    + _BASE_FORMATS
    + (_MEDIA_FORMATS if ENABLE_MEDIA else "")
    + f"{SEP}\n"
    "\U0001f4cb Use /history to see your past conversions."
)

HELP_TEXT = (
    "\U0001f4a1 <b>How to convert a file</b>\n"
    f"{SEP}\n"
    "1. Send or forward a file to this chat.\n"
    "2. Pick a target format from the buttons.\n"
    "3. Wait for the converted file to arrive.\n\n"
    "<b>Commands</b>\n"
    "/start \u2014 supported formats\n"
    "/history \u2014 your last 10 conversions\n"
    "/help \u2014 this message\n\n"
    "Files are converted on the fly and temporary copies are deleted "
    "immediately afterwards."
)

# Telegram deep-link payload: convert_<storage message id>[_<16 hex chars>].
_CONVERT_PAYLOAD_RE = re.compile(r"^convert_(\d{1,15})(?:_([0-9a-f]{16}))?$")


def _expected_signature(message_id: int) -> str:
    payload = f"convert_{message_id}"
    return hmac.new(
        DEEPLINK_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def parse_convert_payload(payload: str):
    """Return the storage message id, or ``None`` when the payload is invalid."""
    if not payload or len(payload) > 64:
        return None

    match = _CONVERT_PAYLOAD_RE.match(payload)
    if not match:
        return None

    message_id = int(match.group(1))
    signature = match.group(2)

    if DEEPLINK_SECRET:
        if not signature or not hmac.compare_digest(
            signature, _expected_signature(message_id)
        ):
            return None
    elif signature:
        # A signed link cannot be trusted if this side has no secret.
        return None
    else:
        # Unsigned links let anyone enumerate storage-channel messages.
        return None

    return message_id


def _extract_file(message):
    if message.document:
        doc = message.document
        return doc, doc.file_name or "document", doc.mime_type, doc.file_size or 0
    if message.audio:
        audio = message.audio
        mime = audio.mime_type
        ext = (mime or "audio/mpeg").split("/")[-1]
        return audio, audio.file_name or f"audio.{ext}", mime, audio.file_size or 0
    if message.video:
        video = message.video
        mime = video.mime_type or "video/mp4"
        ext = mime.split("/")[-1]
        return video, video.file_name or f"video.{ext}", mime, video.file_size or 0
    if message.photo:
        photo = message.photo[-1]
        return photo, "photo.jpg", "image/jpeg", photo.file_size or 0
    return None, None, None, 0


async def _handle_convert_deeplink(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str
) -> None:
    message_id = parse_convert_payload(payload)
    if message_id is None:
        await update.message.reply_text(
            "\u274c <b>Invalid link</b>\n"
            "This conversion link is malformed or has expired.\n"
            "Download the book again from the library.",
            parse_mode="HTML",
        )
        return

    if not STORAGE_CHANNEL_ID:
        await update.message.reply_text(
            "\u26a0\ufe0f <b>Bridge not configured</b>\n"
            "Ask the administrator to set <code>STORAGE_CHANNEL_ID</code>.",
            parse_mode="HTML",
        )
        return

    try:
        copied = await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=STORAGE_CHANNEL_ID,
            message_id=message_id,
        )
    except BadRequest as exc:
        logger.warning("Deep-link copy failed for message %s: %s", message_id, exc)
        await update.message.reply_text(
            "\u274c <b>File unavailable</b>\n"
            "It may have been removed from storage. Download the book again.",
            parse_mode="HTML",
        )
        return
    except Exception as exc:
        logger.exception("Unexpected deep-link copy error: %s", exc)
        await update.message.reply_text(
            "\u274c Could not fetch the file. Please try again.",
        )
        return

    file_obj, file_name, mime_type, file_size = _extract_file(copied)
    if not file_obj:
        await update.message.reply_text(
            "\u274c The stored message is not a convertible file.",
        )
        return

    await send_format_options(
        copied,
        context,
        file_obj,
        file_name,
        mime_type,
        file_size,
        user_id=update.effective_user.id,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        payload = context.args[0].strip() if context.args else ""
        if payload.startswith("convert_"):
            await _handle_convert_deeplink(update, context, payload)
            return
        await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML")
    except Exception as exc:
        logger.error(
            "Unhandled error in start_command: %s\n%s", exc, traceback.format_exc()
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")
