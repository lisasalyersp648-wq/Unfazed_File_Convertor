import asyncio
import os
import logging
from PIL import Image

from config import ENABLE_MEDIA, cpu_executor
from . import ConversionError

logger = logging.getLogger(__name__)

# Guard against decompression bombs (huge dimensions with tiny file size).
MAX_IMAGE_PIXELS = 60_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


async def convert(input_path: str, output_path: str, source_format: str = None, target_format: str = None) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(cpu_executor, _convert_sync, input_path, output_path)


def _convert_sync(input_path: str, output_path: str) -> None:
    source_format = _get_format(input_path)
    target_format = _get_format(output_path)

    try:
        if source_format == "gif" and target_format == "mp4":
            if not ENABLE_MEDIA:
                raise ConversionError(
                    "Animated GIF to MP4 requires ffmpeg, which is not enabled "
                    "in this deployment."
                )
            from converters.video import _convert_sync as video_convert_sync
            video_convert_sync(input_path, output_path)
            return

        with Image.open(input_path) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise ConversionError(
                    f"Image is too large ({img.width}x{img.height}, "
                    f"{img.width * img.height:,} pixels). The limit is "
                    f"{MAX_IMAGE_PIXELS:,} pixels."
                )
            if target_format in ("jpg", "jpeg", "bmp", "pdf") and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            if target_format == "pdf":
                img.save(output_path, "PDF", resolution=100.0)
            elif target_format in ("jpg", "jpeg"):
                img.save(output_path, "JPEG", quality=85, optimize=True)
            elif target_format == "webp":
                img.save(output_path, "WEBP", quality=85)
            elif target_format == "tiff":
                img.save(output_path, "TIFF", compression="tiff_deflate")
            elif target_format == "gif":
                img.save(output_path, "GIF")
            elif target_format == "png":
                img.save(output_path, "PNG", optimize=True)
            elif target_format == "bmp":
                img.save(output_path, "BMP")
            else:
                img.save(output_path)

    except ConversionError:
        raise
    except Image.DecompressionBombError as e:
        raise ConversionError("Image is too large to process safely.") from e
    except Exception as e:
        raise ConversionError(f"Image conversion failed: {e}")


def _get_format(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()
