import asyncio
import os
import logging

from config import ENABLE_MEDIA, FFMPEG_BIN
from . import ConversionError

logger = logging.getLogger(__name__)

FORMAT_MAP = {
    "mp3":  ("libmp3lame",  "mp3",  ["-b:a", "192k"]),
    "ogg":  ("libvorbis",   "ogg",  ["-q:a", "5"]),
    "wav":  ("pcm_s16le",   "wav",  []),
    "flac": ("flac",        "flac", []),
    "m4a":  ("aac",         "mp4",  ["-b:a", "192k"]),
    "opus": ("libopus",     "opus", []),
}

INPUT_FORMAT_MAP = {
    "m4a": "mp4",
}


async def _run_ffmpeg(args, timeout=300):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ConversionError(f"FFmpeg timed out after {timeout}s")
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip()
        raise ConversionError(f"Audio conversion failed: {msg}")
    return stdout, stderr


async def convert(input_path: str, output_path: str, source_format: str = None, target_format: str = None) -> None:
    if not ENABLE_MEDIA:
        raise ConversionError(
            "Audio conversion requires ffmpeg, which is not enabled in this deployment."
        )
    src = _get_format(input_path)
    tgt = _get_format(output_path)

    codec, fmt, extra = FORMAT_MAP.get(tgt, (None, None, []))
    if codec is None:
        raise ConversionError(f"Unsupported target format: .{tgt}")

    in_fmt = INPUT_FORMAT_MAP.get(src)
    cmd = [FFMPEG_BIN, "-y", "-threads", "0"]
    if in_fmt:
        cmd.extend(["-f", in_fmt])
    cmd.extend(["-i", input_path])
    cmd.extend(["-acodec", codec])
    if extra:
        cmd.extend(extra)
    cmd.extend(["-f", fmt, output_path])

    try:
        await _run_ffmpeg(cmd)
    except ConversionError:
        raise
    except Exception as e:
        raise ConversionError(f"Audio conversion failed: {e}")


def _get_format(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()
