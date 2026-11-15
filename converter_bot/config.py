"""Runtime configuration.

All values come from environment variables so the same image/code can run
locally, in a container, or on Vercel. ``.env`` is loaded for local
development only; it is excluded from deployments.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

# Load an explicit file when requested, otherwise the `.env` in the current
# working directory.  This keeps `python bot.py` (run from converter_bot/) and
# `uvicorn app:app` (run from the repository root) deterministic.
_DOTENV_PATH = (os.getenv("DOTENV_PATH") or "").strip() or os.path.join(
    os.getcwd(), ".env"
)
load_dotenv(_DOTENV_PATH)

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%r; using %s", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Platform / runtime mode
# ---------------------------------------------------------------------------

VERCEL_ENV = (os.getenv("VERCEL_ENV") or "").strip()
IS_VERCEL = bool(os.getenv("VERCEL")) or bool(VERCEL_ENV)

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()
if LOG_LEVEL not in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
    LOG_LEVEL = "INFO"

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN is not set. Add it to .env (local) or the environment "
        "(production). Get a token from @BotFather."
    )
if ":" not in BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN looks malformed. Expected the '<bot_id>:<secret>' format "
        "issued by @BotFather."
    )

# Optional local Bot API server (self-hosted only; not available on Vercel).
LOCAL_API_URL = "" if IS_VERCEL else (os.getenv("LOCAL_API_URL") or "").rstrip("/")
LOCAL_API_MODE = _env_bool("LOCAL_API_MODE", False) and bool(LOCAL_API_URL)

ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]

# ---------------------------------------------------------------------------
# Webhook (Vercel / standalone webhook servers)
# ---------------------------------------------------------------------------

WEBHOOK_PATH = (os.getenv("WEBHOOK_PATH") or "/api/webhook").strip()
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or "").rstrip("/")
if not WEBHOOK_URL and IS_VERCEL and VERCEL_ENV == "production":
    # Production deployments can discover their stable domain automatically.
    host = (
        os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
        or os.getenv("VERCEL_BRANCH_URL")
        or os.getenv("VERCEL_URL")
        or ""
    ).strip()
    if host:
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        WEBHOOK_URL = f"{host.rstrip('/')}{WEBHOOK_PATH}"

WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "").strip()
AUTO_SET_WEBHOOK = _env_bool(
    "AUTO_SET_WEBHOOK", bool(WEBHOOK_URL) and IS_VERCEL
)

if WEBHOOK_URL and not WEBHOOK_SECRET:
    raise ValueError(
        "WEBHOOK_SECRET must be set when WEBHOOK_URL is configured. Generate "
        "one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    )

if WEBHOOK_SECRET and len(WEBHOOK_SECRET) < 16:
    raise ValueError("WEBHOOK_SECRET must be at least 16 characters long.")

if WEBHOOK_SECRET and not re.fullmatch(r"[A-Za-z0-9_-]+", WEBHOOK_SECRET):
    raise ValueError(
        "WEBHOOK_SECRET may only contain letters, digits, underscores, and "
        "hyphens (Telegram's secret-token rules)."
    )

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

# Prefer a managed Postgres instance. Vercel/Neon expose several aliases.
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("POSTGRES_PRISMA_URL")
    or os.getenv("POSTGRES_URL_NON_POOLING")
    or ""
).strip()

DATABASE_SSL = (os.getenv("DATABASE_SSL") or "").strip() or None
DB_POOL_SIZE = max(1, _env_int("DB_POOL_SIZE", 2))

# SQLite is a local fallback only. On Vercel the filesystem is ephemeral, so
# history/cache will not survive cold starts without DATABASE_URL.
DEFAULT_DB_PATH = os.path.join(tempfile.gettempdir(), "converter_bot.db") if IS_VERCEL else "converter_bot.db"
DB_PATH = (os.getenv("DB_PATH") or DEFAULT_DB_PATH).strip()

# ---------------------------------------------------------------------------
# Filesystem / concurrency
# ---------------------------------------------------------------------------

MAX_WORKERS = _env_int("MAX_WORKERS", 0)
_cpus = os.cpu_count() or 1
if MAX_WORKERS <= 0:
    MAX_WORKERS = min(32, _cpus + 4)

cpu_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="convert")
io_executor = ThreadPoolExecutor(max_workers=min(8, max(2, _cpus * 2)), thread_name_prefix="io")

if IS_VERCEL:
    # /tmp is the only writable location in a Vercel Function.
    TEMP_DIR = "/tmp"
else:
    TEMP_DIR = (os.getenv("TEMP_DIR") or "").strip() or tempfile.gettempdir()
    if not os.getenv("TEMP_DIR") and os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        TEMP_DIR = "/dev/shm"

WORK_DIR = os.path.join(TEMP_DIR, "bot_workdir")
os.makedirs(WORK_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Conversion engines
# ---------------------------------------------------------------------------

def _resolve_ffmpeg() -> str:
    explicit = (os.getenv("FFMPEG_BIN") or "").strip()
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # optional bundled binary (large; not installed on Vercel by default)
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


FFMPEG_BIN = _resolve_ffmpeg()


def _has_ffmpeg() -> bool:
    if os.path.isabs(FFMPEG_BIN):
        return os.path.isfile(FFMPEG_BIN)
    return shutil.which(FFMPEG_BIN) is not None


HAS_FFMPEG = _has_ffmpeg()
ENABLE_MEDIA = _env_bool("ENABLE_MEDIA", HAS_FFMPEG)

# Hard limits. Telegram's cloud Bot API can only download files up to 20 MB;
# a local Bot API server removes that cap.
TELEGRAM_FILE_LIMIT_MB = 20
MAX_INPUT_MB = _env_int("MAX_INPUT_MB", 0)  # 0 = use per-category limits
CONVERSION_TIMEOUT = max(30, _env_int("CONVERSION_TIMEOUT", 240))

# ---------------------------------------------------------------------------
# Library Bot deep-link bridge
# ---------------------------------------------------------------------------

try:
    STORAGE_CHANNEL_ID = int((os.getenv("STORAGE_CHANNEL_ID", "") or "0").strip() or 0)
except ValueError:
    STORAGE_CHANNEL_ID = 0

DEEPLINK_SECRET = (os.getenv("DEEPLINK_SECRET") or "").strip()

if STORAGE_CHANNEL_ID and not DEEPLINK_SECRET:
    raise ValueError(
        "STORAGE_CHANNEL_ID is set but DEEPLINK_SECRET is empty. Deep links "
        "would be forgeable; set the same DEEPLINK_SECRET in both bots."
    )
