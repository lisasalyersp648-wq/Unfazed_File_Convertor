"""Callback handler: download, convert, persist, and upload the result."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
import time
from pathlib import Path

from telegram import InputFile, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import db
from config import (
    CONVERSION_TIMEOUT,
    ENABLE_MEDIA,
    LOCAL_API_URL,
    TELEGRAM_FILE_LIMIT_MB,
    TEMP_DIR,
)
from converters import ConversionError, supports
from converters.audio import convert as audio_convert
from converters.document import convert as doc_convert
from converters.image import convert as img_convert
from converters.video import convert as video_convert
from utils import (
    _compute_file_hash,
    check_file_size,
    cleanup_temp_dir,
    clear_format_cache,
    detect_format,
    download_with_extension,
    format_size,
    get_category,
    get_limit_mb,
)

logger = logging.getLogger(__name__)

TELEGRAM_FILE_LIMIT = TELEGRAM_FILE_LIMIT_MB * 1024 * 1024
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # Bot API upload cap (cloud)

_max_concurrent = max(1, int(os.getenv("MAX_CONCURRENT_CONVERSIONS", "4")))
_semaphore = asyncio.Semaphore(_max_concurrent)

CATEGORY_CONVERTERS = {
    "document": doc_convert,
    "image": img_convert,
    "audio": audio_convert,
    "video": video_convert,
}

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


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _label(src: str, tgt: str) -> str:
    src_emoji = FORMAT_EMOJI.get(src, "")
    tgt_emoji = FORMAT_EMOJI.get(tgt, "")
    return f"{src_emoji} {src.upper()} \u2192 {tgt_emoji} {tgt.upper()}"


def _safe_stem(name: str) -> str:
    stem = Path(name).stem.strip().strip(".")
    # Strip characters that are awkward in headers/file systems.
    stem = "".join(ch for ch in stem if ch.isalnum() or ch in (" ", "-", "_", ".")).strip()
    return stem[:80] or "converted"


async def _edit_safe(query, text: str, **kwargs) -> None:
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Could not edit callback message: %s", exc)
        try:
            if query.message:
                await query.message.reply_text(text, **kwargs)
        except Exception:
            pass


async def _record_failure(
    user_id: int,
    username: str,
    original_filename: str,
    source_format: str,
    target_format: str,
    error_msg: str,
) -> None:
    try:
        await db.add_job(
            user_id=user_id,
            username=username,
            original_filename=original_filename,
            source_format=source_format,
            target_format=target_format,
            status="failed",
            error_msg=error_msg,
        )
    except Exception:
        logger.exception("Could not record failed job")


async def handle_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except BadRequest:
        # The callback query can be old when Telegram retries a slow delivery.
        pass

    parts = query.data.split(":") if query.data else []
    if len(parts) != 4:
        await _edit_safe(query, "Invalid conversion request.")
        return

    _, source_format, target_format, file_key = parts
    user = update.effective_user
    if user is None:
        await _edit_safe(query, "Could not identify your account. Please try again.")
        return

    user_id = user.id
    username = user.username or ""
    chat_id = query.message.chat_id if query.message else update.effective_chat.id

    # Claiming deletes the token atomically, so a Telegram webhook retry cannot
    # start a second conversion for the same button press.
    entry = await db.claim_pending_file(file_key, user_id)
    if not entry:
        await _edit_safe(
            query,
            "This selection expired or was already converted. "
            "Please send the file again.",
        )
        return

    file_id = entry["file_id"]
    original_filename = entry.get("file_name") or f"file.{source_format}"
    file_size = int(entry.get("file_size") or 0)

    if not supports(source_format, target_format):
        await _edit_safe(query, "That conversion is not available in this deployment.")
        return

    fmt_label = _label(source_format, target_format)

    if not LOCAL_API_URL and file_size and file_size > TELEGRAM_FILE_LIMIT:
        await _edit_safe(
            query,
            f"\u274c <b>File too large</b>\n{SEP}\n{fmt_label}\n{SEP}\n"
            f"Telegram's cloud Bot API cannot download files larger than "
            f"{TELEGRAM_FILE_LIMIT_MB} MB.",
            parse_mode="HTML",
        )
        return

    if not ENABLE_MEDIA and get_category(source_format) in ("audio", "video"):
        await _edit_safe(
            query,
            "\u274c Audio/video conversion is not enabled on this deployment.",
            parse_mode="HTML",
        )
        return

    await _edit_safe(
        query,
        f"\u23f3 <b>Converting</b>\n{SEP}\n{fmt_label}\n{SEP}",
        parse_mode="HTML",
    )

    temp_dir = None
    try:
        async with _semaphore:
            temp_dir = tempfile.mkdtemp(
                prefix=f"converter_{user_id}_{int(time.time())}_",
                dir=TEMP_DIR,
            )

            stem = _safe_stem(original_filename)
            output_name = f"{stem}.{target_format}"
            output_path = os.path.join(temp_dir, output_name)

            try:
                tg_file = await context.bot.get_file(file_id)
            except Exception as exc:
                if "File is too big" in str(exc):
                    raise ConversionError(
                        "Telegram's Bot API cannot download files larger than "
                        f"{TELEGRAM_FILE_LIMIT_MB} MB."
                    ) from exc
                raise

            input_path = await download_with_extension(
                tg_file,
                source_format,
                temp_dir,
                file_id=file_id if LOCAL_API_URL else None,
            )

            actual_format = detect_format(input_path, original_filename)
            aliases = {"jpg": "jpeg"}
            if (
                actual_format
                and aliases.get(actual_format, actual_format)
                != aliases.get(source_format, source_format)
            ):
                raise ConversionError(
                    f"Expected {source_format.upper()}, detected "
                    f"{actual_format.upper()}. The file content does not match "
                    "its name."
                )

            file_hash = _compute_file_hash(input_path)

            cached_file_id = await db.find_cached_job(file_hash, target_format)
            if cached_file_id:
                try:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=cached_file_id,
                        caption=f"\u267b <b>Cached</b> {fmt_label}",
                        parse_mode="HTML",
                    )
                    await _edit_safe(
                        query,
                        f"\u2705 <b>Already converted!</b>\n{SEP}\n{fmt_label}",
                        parse_mode="HTML",
                    )
                    await db.add_job(
                        user_id=user_id,
                        username=username,
                        original_filename=original_filename,
                        source_format=source_format,
                        target_format=target_format,
                        status="success",
                        input_hash=file_hash,
                    )
                    return
                except Exception as exc:
                    logger.warning("Cached file_id is no longer usable: %s", exc)

            if not check_file_size(input_path, source_format):
                size = format_size(input_path)
                limit_mb = get_limit_mb(source_format)
                raise ConversionError(
                    f"File too large ({size}). Maximum for "
                    f"{source_format.upper()}: {limit_mb} MB"
                )

            category = get_category(source_format)
            converter_func = CATEGORY_CONVERTERS.get(category)
            if not converter_func:
                raise ConversionError(f"Can't convert {source_format.upper()} files.")

            await asyncio.wait_for(
                converter_func(
                    input_path, output_path, source_format, target_format
                ),
                timeout=CONVERSION_TIMEOUT,
            )

            out_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            if out_size == 0:
                raise ConversionError("Conversion produced an empty file.")

            if not LOCAL_API_URL and out_size > TELEGRAM_UPLOAD_LIMIT:
                raise ConversionError(
                    f"The converted file is {_format_bytes(out_size)}, which "
                    "exceeds Telegram's 50 MB upload limit. Try a smaller file."
                )

            out_size_str = _format_bytes(out_size)

            success_lines = [
                "\u2705 <b>Conversion complete</b>",
                SEP,
                fmt_label,
                f"Size: <code>{out_size_str}</code>",
                SEP,
            ]
            await _edit_safe(query, "\n".join(success_lines), parse_mode="HTML")

            caption = f"\u2705 <b>Done!</b> {fmt_label} \u00b7 <code>{out_size_str}</code>"

            try:
                with open(output_path, "rb") as handle:
                    message = await context.bot.send_document(
                        chat_id=chat_id,
                        document=InputFile(handle, filename=output_name),
                        caption=caption,
                        parse_mode="HTML",
                        read_timeout=300,
                        write_timeout=300,
                    )
                if message.document:
                    await db.update_job_file_id_by_hash(
                        file_hash, message.document.file_id
                    )
            except Exception as exc:
                logger.warning("Failed to send converted document: %s", exc)
                raise ConversionError(
                    "The conversion succeeded, but Telegram rejected the upload. "
                    "Please try again."
                ) from exc

            await db.add_job(
                user_id=user_id,
                username=username,
                original_filename=original_filename,
                source_format=source_format,
                target_format=target_format,
                status="success",
                input_hash=file_hash,
            )

    except asyncio.TimeoutError:
        await _edit_safe(
            query,
            f"\u274c <b>Conversion timed out</b>\n{SEP}\n{fmt_label}\n{SEP}\n"
            f"Please try a smaller or simpler file.",
            parse_mode="HTML",
        )
        await _record_failure(
            user_id, username, original_filename, source_format, target_format,
            f"Timed out after {CONVERSION_TIMEOUT}s",
        )

    except ConversionError as exc:
        logger.warning("Conversion failed for user %s: %s", user_id, exc)
        error_text = html.escape(str(exc))
        await _edit_safe(
            query,
            f"\u274c <b>Conversion failed</b>\n{SEP}\n{fmt_label}\n{SEP}\n"
            f"<code>{error_text}</code>",
            parse_mode="HTML",
        )
        await _record_failure(
            user_id, username, original_filename, source_format, target_format,
            str(exc),
        )

    except Exception as exc:
        logger.exception("Unexpected error during conversion for user %s", user_id)
        await _edit_safe(
            query,
            f"\u274c <b>Something went wrong</b>\n{SEP}\nPlease try again.",
            parse_mode="HTML",
        )
        await _record_failure(
            user_id, username, original_filename, source_format, target_format,
            str(exc),
        )

    finally:
        if temp_dir:
            cleanup_temp_dir(temp_dir)
        clear_format_cache()
