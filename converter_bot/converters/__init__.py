"""Conversion capability map.

The Vercel deployment uses pure-Python engines only (PyMuPDF, python-docx,
mammoth, Pillow). Audio/video targets are hidden unless ffmpeg is available
and ``ENABLE_MEDIA`` is on.
"""

from __future__ import annotations

from config import ENABLE_MEDIA


class ConversionError(Exception):
    """Raised when a conversion cannot be completed."""


AUDIO_FORMATS = {"mp3", "ogg", "wav", "flac", "m4a", "opus"}
VIDEO_FORMATS = {"mp4", "mkv", "avi", "mov", "webm"}

# Formats that the pure-Python document engine can produce or consume.
CONVERSION_MAP: dict[str, list[str]] = {
    "pdf": ["docx", "txt", "html", "md", "epub"],
    "docx": ["pdf", "txt", "html", "md", "epub"],
    "epub": ["pdf", "docx", "txt", "html", "md"],
    "txt": ["pdf", "docx", "html", "md", "epub"],
    "html": ["pdf", "docx", "txt", "md", "epub"],
    "md": ["pdf", "docx", "html", "txt", "epub"],
    # Images (Pillow)
    "jpg": ["png", "webp", "pdf", "bmp", "tiff"],
    "jpeg": ["png", "webp", "pdf", "bmp", "tiff"],
    "png": ["jpg", "webp", "pdf", "bmp", "tiff", "gif"],
    "webp": ["jpg", "png", "pdf"],
    "gif": ["png", "mp4"],
    "bmp": ["jpg", "png", "pdf"],
    "tiff": ["jpg", "png", "pdf"],
    # Media (ffmpeg, optional)
    "mp3": ["ogg", "wav", "flac", "m4a", "opus"],
    "ogg": ["mp3", "wav", "flac"],
    "wav": ["mp3", "ogg", "flac", "m4a"],
    "flac": ["mp3", "ogg", "wav", "m4a"],
    "m4a": ["mp3", "ogg", "wav", "flac"],
    "opus": ["mp3", "ogg", "wav"],
    "mp4": ["mkv", "avi", "mov", "webm", "mp3", "gif"],
    "mkv": ["mp4", "avi", "mov", "webm", "mp3"],
    "avi": ["mp4", "mkv", "mov", "webm"],
    "mov": ["mp4", "mkv", "webm"],
    "webm": ["mp4", "mkv"],
}


def available_targets(source_format: str) -> list[str]:
    """Return the targets that are actually usable in this deployment."""
    source_format = (source_format or "").lower().strip(".")
    targets = list(CONVERSION_MAP.get(source_format, []))

    if ENABLE_MEDIA:
        return targets

    if source_format in AUDIO_FORMATS or source_format in VIDEO_FORMATS:
        return []

    return [
        target
        for target in targets
        if target not in AUDIO_FORMATS and target not in VIDEO_FORMATS
    ]


def supports(source_format: str, target_format: str) -> bool:
    return (target_format or "").lower() in available_targets(source_format)
