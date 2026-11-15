"""File helpers shared by handlers and converters."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

import httpx

from config import BOT_TOKEN, LOCAL_API_URL, MAX_INPUT_MB

try:
    import magic

    HAS_MAGIC = True
except ImportError:  # libmagic is not available on Vercel
    HAS_MAGIC = False
    magic = None

logger = logging.getLogger(__name__)

CATEGORY_MAP = {
    "pdf": "document", "docx": "document", "epub": "document",
    "txt": "document", "html": "document", "md": "document",
    "odt": "document", "rtf": "document",
    "mobi": "document", "fb2": "document",
    "jpg": "image", "jpeg": "image", "png": "image", "webp": "image",
    "gif": "image", "bmp": "image", "tiff": "image",
    "mp3": "audio", "ogg": "audio", "wav": "audio", "flac": "audio",
    "m4a": "audio", "opus": "audio",
    "mp4": "video", "mkv": "video", "avi": "video", "mov": "video", "webm": "video",
}

CATEGORY_LIMITS = {
    "document": 50 * 1024 * 1024,
    "image": 30 * 1024 * 1024,
    "audio": 100 * 1024 * 1024,
    "video": 500 * 1024 * 1024,
}

MIME_MAP = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/epub+zip": "epub",
    "text/plain": "txt",
    "text/html": "html",
    "text/markdown": "md",
    "text/x-markdown": "md",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/x-mobipocket-ebook": "mobi",
    "application/x-fictionbook+xml": "fb2",
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/tiff": "tiff",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/opus": "opus",
    "video/mp4": "mp4",
    "video/x-matroska": "mkv",
    "video/x-msvideo": "avi",
    "video/quicktime": "mov",
    "video/webm": "webm",
}

_format_cache: dict[str, Optional[str]] = {}

CHUNK_SIZE = 64 * 1024


async def download_with_extension(
    tg_file: Any,
    detected_format: str,
    temp_dir: str,
    file_id: Optional[str] = None,
) -> str:
    """Download a Telegram file to ``temp_dir`` with the correct extension.

    With a local Bot API server the file may be larger than Telegram's 20 MB
    cloud limit, so we stream it over the local file endpoint instead of using
    ``docker cp`` (which only works from the host).
    """
    fd, correct_path = tempfile.mkstemp(suffix=f".{detected_format}", dir=temp_dir)
    os.close(fd)

    if LOCAL_API_URL and file_id:
        await _download_from_local_api(file_id, correct_path)
    else:
        # Streams to disk instead of buffering the whole file in RAM.
        await tg_file.download_to_drive(custom_path=correct_path)

    size = os.path.getsize(correct_path)
    logger.info("Downloaded file to %s (%s bytes)", correct_path, size)
    if size == 0:
        os.unlink(correct_path)
        raise RuntimeError(
            "Downloaded file is empty. It may have expired on Telegram's servers."
        )
    return correct_path


async def _download_from_local_api(file_id: str, destination: str) -> None:
    timeout = httpx.Timeout(connect=30.0, read=None, write=None, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{LOCAL_API_URL}/bot{BOT_TOKEN}/getFile",
            json={"file_id": file_id},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"getFile failed: {payload.get('description')}")
        raw_path = payload["result"]["file_path"]

        url = f"{LOCAL_API_URL}/file/bot{BOT_TOKEN}/{raw_path.lstrip('/')}"
        async with client.stream("GET", url) as stream:
            stream.raise_for_status()
            with open(destination, "wb") as output:
                async for chunk in stream.aiter_bytes(1024 * 1024):
                    output.write(chunk)


def _compute_file_hash(file_path: str, max_bytes: int = 524288) -> str:
    """Cheap content fingerprint used for the conversion cache."""
    digest = hashlib.md5()
    size = os.path.getsize(file_path)
    with open(file_path, "rb") as handle:
        digest.update(handle.read(max_bytes))
    digest.update(str(size).encode())
    return digest.hexdigest()


def iterfile(file_path: str, chunk_size: int = CHUNK_SIZE):
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield chunk


FILE_SIGNATURES = [
    ("pdf", bytes([0x25, 0x50, 0x44, 0x46])),
    ("png", bytes([0x89, 0x50, 0x4E, 0x47])),
    ("jpg", bytes([0xFF, 0xD8, 0xFF])),
    ("gif", bytes([0x47, 0x49, 0x46, 0x38])),
    ("bmp", bytes([0x42, 0x4D])),
    ("zip", bytes([0x50, 0x4B])),
    ("rtf", b"{\\rtf"),
    ("flac", b"fLaC"),
    ("ogg", b"OggS"),
    ("mkv", bytes([0x1A, 0x45, 0xDF, 0xA3])),
    ("mp3", b"ID3"),
    ("tiff", bytes([0x49, 0x49, 0x2A, 0x00])),
    ("tiff", bytes([0x4D, 0x4D, 0x00, 0x2A])),
]

SIGNATURE_OFFSETS = {
    "webp": (8, b"WEBP"),
    "wav": (8, b"WAVE"),
    "mp4": (4, b"ftyp"),
}


def detect_format_by_signature(file_path: str) -> Optional[str]:
    if file_path in _format_cache:
        return _format_cache[file_path]

    try:
        with open(file_path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return None

    for fmt, signature in FILE_SIGNATURES:
        if head.startswith(signature):
            if fmt == "zip":
                result = _detect_zip_subtype(file_path)
            elif fmt == "mp3" and not head.startswith(b"ID3"):
                result = "mp3"
            else:
                result = fmt
            _format_cache[file_path] = result
            return result

    for fmt, (offset, signature) in SIGNATURE_OFFSETS.items():
        if len(head) > offset and head[offset:offset + len(signature)] == signature:
            _format_cache[file_path] = fmt
            return fmt

    _format_cache[file_path] = None
    return None


def _detect_zip_subtype(file_path: str) -> Optional[str]:
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = archive.namelist()
            if "mimetype" in names:
                mime = archive.read("mimetype").decode("utf-8", errors="replace").strip()
                if mime == "application/epub+zip":
                    return "epub"
            lowered = {name.lower() for name in names}
            if "word/document.xml" in lowered:
                return "docx"
            if "content.xml" in lowered:
                return "odt"
        return "zip"
    except Exception:
        return "zip"


def detect_format(file_path: str, original_filename: str) -> Optional[str]:
    if file_path in _format_cache:
        return _format_cache[file_path]

    signature = detect_format_by_signature(file_path)
    if signature and signature != "zip":
        return signature

    if HAS_MAGIC:
        try:
            mime = magic.from_file(file_path, mime=True)
            if mime in MIME_MAP:
                result = MIME_MAP[mime]
                _format_cache[file_path] = result
                return result
        except Exception as exc:
            logger.debug("python-magic failed for %s: %s", file_path, exc)

    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""
    if ext in CATEGORY_MAP:
        _format_cache[file_path] = ext
        return ext

    return None


def check_file_size(file_path: str, source_format: str) -> bool:
    size = os.path.getsize(file_path)
    if MAX_INPUT_MB:
        return size <= MAX_INPUT_MB * 1024 * 1024
    category = CATEGORY_MAP.get(source_format)
    if not category:
        return True
    limit = CATEGORY_LIMITS.get(category, float("inf"))
    return size <= limit


def get_limit_mb(source_format: str) -> Optional[int]:
    if MAX_INPUT_MB:
        return MAX_INPUT_MB
    category = CATEGORY_MAP.get(source_format)
    if not category:
        return None
    limit = CATEGORY_LIMITS.get(category)
    return limit // (1024 * 1024) if limit else None


def format_size(file_path: str) -> str:
    size = os.path.getsize(file_path)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def get_category(source_format: str) -> Optional[str]:
    return CATEGORY_MAP.get(source_format)


def cleanup_temp_dir(temp_dir: str) -> None:
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as exc:
        logger.warning("Failed to clean up temp dir %s: %s", temp_dir, exc)


def clear_format_cache() -> None:
    _format_cache.clear()
