import asyncio
import os
import logging

from config import ENABLE_MEDIA, FFMPEG_BIN
from . import ConversionError

logger = logging.getLogger(__name__)


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
        raise ConversionError(f"FFmpeg error: {msg}")
    return stdout, stderr


def _ffmpeg_base(input_path, output_path, extra_args=None):
    cmd = [FFMPEG_BIN, "-y", "-threads", "0", "-i", input_path]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend([output_path])
    return cmd


async def convert(input_path: str, output_path: str, source_format: str = None, target_format: str = None) -> None:
    if not ENABLE_MEDIA:
        raise ConversionError(
            "Video conversion requires ffmpeg, which is not enabled in this deployment."
        )
    tgt = _get_format(output_path)
    try:
        if tgt == "mp3":
            await _extract_audio(input_path, output_path)
        elif tgt == "gif":
            await _video_to_gif(input_path, output_path)
        else:
            await _video_to_video(input_path, output_path, tgt)
    except ConversionError:
        raise
    except Exception as e:
        raise ConversionError(f"Video conversion failed: {e}")


def _get_format(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


async def _extract_audio(input_path: str, output_path: str) -> None:
    args = _ffmpeg_base(input_path, output_path, [
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "128k",
        "-compression_level", "0",
        "-f", "mp3",
    ])
    await _run_ffmpeg(args)


async def _video_to_gif(input_path: str, output_path: str) -> None:
    args = _ffmpeg_base(input_path, output_path, [
        "-vf", "fps=15,scale=480:-1",
        "-loop", "0",
    ])
    await _run_ffmpeg(args)


def _run_ffmpeg_sync(args, timeout=300):
    import subprocess
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError(f"FFmpeg timed out after {timeout}s")
    if result.returncode != 0:
        msg = result.stderr.decode("utf-8", errors="replace").strip()
        raise ConversionError(f"FFmpeg error: {msg}")
    return result.stdout, result.stderr


def _convert_sync(input_path: str, output_path: str) -> None:
    tgt = _get_format(output_path)
    try:
        if tgt == "mp3":
            args = _ffmpeg_base(input_path, output_path, [
                "-vn", "-acodec", "libmp3lame", "-b:a", "128k",
                "-compression_level", "0", "-f", "mp3",
            ])
            _run_ffmpeg_sync(args)
        elif tgt == "gif":
            args = _ffmpeg_base(input_path, output_path, [
                "-vf", "fps=15,scale=480:-1", "-loop", "0",
            ])
            _run_ffmpeg_sync(args)
        else:
            args = _ffmpeg_base(input_path, output_path, [
                "-c:v", "copy", "-c:a", "copy",
            ])
            try:
                _run_ffmpeg_sync(args)
            except ConversionError:
                logger.info("Stream copy failed, re-encoding video")
                extra = ["-c:v", "libx264", "-preset", "fast", "-c:a", "aac"]
                if tgt == "mp4":
                    extra.extend(["-movflags", "+faststart"])
                args = _ffmpeg_base(input_path, output_path, extra)
                _run_ffmpeg_sync(args)
    except ConversionError:
        raise
    except Exception as e:
        raise ConversionError(f"Video conversion failed: {e}")


async def _video_to_video(input_path: str, output_path: str, target_format: str) -> None:
    args = _ffmpeg_base(input_path, output_path, [
        "-c:v", "copy",
        "-c:a", "copy",
    ])
    try:
        await _run_ffmpeg(args)
    except ConversionError:
        logger.info("Stream copy failed, re-encoding video")
        extra = [
            "-c:v", "libx264",
            "-preset", "fast",
            "-c:a", "aac",
        ]
        if target_format == "mp4":
            extra.extend(["-movflags", "+faststart"])
        args = _ffmpeg_base(input_path, output_path, extra)
        await _run_ffmpeg(args)
