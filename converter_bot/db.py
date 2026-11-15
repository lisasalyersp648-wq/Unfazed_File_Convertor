"""Persistence layer.

Postgres is used whenever ``DATABASE_URL`` (or a Vercel/Neon alias) is set.
SQLite remains the zero-config local fallback. Both implementations expose the
same small async API so handlers do not care which backend is active.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiosqlite

from config import DATABASE_SSL, DATABASE_URL, DB_PATH, DB_POOL_SIZE

logger = logging.getLogger(__name__)

_pg_pool: Any = None
_sqlite: Optional[aiosqlite.Connection] = None
_is_postgres = bool(DATABASE_URL)

_SQLITE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        original_filename TEXT,
        source_format TEXT,
        target_format TEXT,
        status TEXT,
        error_msg TEXT,
        input_hash TEXT,
        output_file_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_cache ON jobs (input_hash, target_format, status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs (user_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS pending_files (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        file_name TEXT,
        file_size INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_files (user_id)",
]

_PG_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        username TEXT,
        original_filename TEXT,
        source_format TEXT,
        target_format TEXT,
        status TEXT,
        error_msg TEXT,
        input_hash TEXT,
        output_file_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_cache ON jobs (input_hash, target_format, status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs (user_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS pending_files (
        token TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        file_id TEXT NOT NULL,
        file_name TEXT,
        file_size BIGINT DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_files (user_id)",
]


def backend_name() -> str:
    return "postgres" if _is_postgres else "sqlite"


def _prepare_postgres_dsn(url: str) -> tuple[str, bool]:
    """Normalise a managed-Postgres URL for asyncpg.

    asyncpg does not understand every libpq query parameter (notably
    ``sslmode`` and ``channel_binding``), so strip them and translate SSL into
    the ``ssl`` argument instead.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    sslmode = (query.pop("sslmode", [None])[0] or "").lower()

    # PgBouncer/Neon pooler parameters that asyncpg must not forward.
    for key in (
        "channel_binding",
        "connect_timeout",
        "target_session_attrs",
        "pgbouncer",
        "options",
        "application_name",
    ):
        query.pop(key, None)

    clean_query = urlencode({k: v[0] for k, v in query.items() if v})
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment))

    if DATABASE_SSL is not None:
        ssl: bool = DATABASE_SSL.lower() in ("1", "true", "yes", "on", "require")
    elif sslmode in ("require", "verify-ca", "verify-full"):
        ssl = True
    elif sslmode == "disable":
        ssl = False
    else:
        host = (parts.hostname or "").lower()
        ssl = host not in ("", "localhost", "127.0.0.1", "::1")

    return clean, ssl


async def init() -> None:
    """Create the connection/pool and ensure the schema exists."""
    global _pg_pool, _sqlite

    if _is_postgres:
        if _pg_pool is not None:
            return
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - config error
            raise RuntimeError(
                "DATABASE_URL is set but asyncpg is not installed. "
                "Run: pip install asyncpg"
            ) from exc

        dsn, ssl = _prepare_postgres_dsn(DATABASE_URL)
        _pg_pool = await asyncpg.create_pool(
            dsn=dsn,
            ssl=ssl,
            min_size=1,
            max_size=DB_POOL_SIZE,
            timeout=30,
            command_timeout=30,
            max_inactive_connection_lifetime=60,
            # Neon/Supabase poolers run PgBouncer, which does not support
            # asyncpg's server-side prepared statement cache.
            statement_cache_size=0,
        )
        async with _pg_pool.acquire() as conn:
            for statement in _PG_SCHEMA:
                await conn.execute(statement)
            # Forward-compatible migrations for databases created by older
            # versions of the bot.
            for column in ("input_hash", "output_file_id"):
                await conn.execute(
                    f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {column} TEXT"
                )
        logger.info("Postgres ready (pool size %s)", DB_POOL_SIZE)
        return

    if _sqlite is not None:
        return

    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    _sqlite = await aiosqlite.connect(DB_PATH)
    _sqlite.row_factory = aiosqlite.Row
    await _sqlite.execute("PRAGMA journal_mode=WAL")
    await _sqlite.execute("PRAGMA synchronous=NORMAL")
    for statement in _SQLITE_SCHEMA:
        await _sqlite.execute(statement)
    for column in ("input_hash", "output_file_id"):
        try:
            await _sqlite.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        except Exception:
            pass
    await _sqlite.commit()
    logger.info("SQLite ready at %s", DB_PATH)


def _require_ready() -> None:
    if _is_postgres and _pg_pool is None:
        raise RuntimeError("Database not initialised. Call db.init() first.")
    if not _is_postgres and _sqlite is None:
        raise RuntimeError("Database not initialised. Call db.init() first.")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


async def add_job(
    user_id: int,
    username: str,
    original_filename: str,
    source_format: str,
    target_format: str,
    status: str,
    error_msg: str | None = None,
    input_hash: str | None = None,
) -> int | None:
    _require_ready()
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO jobs
                    (user_id, username, original_filename, source_format,
                     target_format, status, error_msg, input_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                user_id,
                username,
                original_filename,
                source_format,
                target_format,
                status,
                error_msg,
                input_hash,
            )

    cursor = await _sqlite.execute(
        """
        INSERT INTO jobs
            (user_id, username, original_filename, source_format, target_format,
             status, error_msg, input_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            original_filename,
            source_format,
            target_format,
            status,
            error_msg,
            input_hash,
        ),
    )
    await _sqlite.commit()
    return cursor.lastrowid


async def find_cached_job(input_hash: str, target_format: str) -> str | None:
    _require_ready()
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT output_file_id FROM jobs
                WHERE input_hash = $1 AND target_format = $2
                  AND status = 'success' AND output_file_id IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                input_hash,
                target_format,
            )
            return row["output_file_id"] if row else None

    cursor = await _sqlite.execute(
        """
        SELECT output_file_id FROM jobs
        WHERE input_hash = ? AND target_format = ?
          AND status = 'success' AND output_file_id IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (input_hash, target_format),
    )
    row = await cursor.fetchone()
    return row["output_file_id"] if row else None


async def update_job_file_id(job_id: int, file_id: str) -> None:
    _require_ready()
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET output_file_id = $1 WHERE id = $2", file_id, job_id
            )
        return

    await _sqlite.execute(
        "UPDATE jobs SET output_file_id = ? WHERE id = ?", (file_id, job_id)
    )
    await _sqlite.commit()


async def update_job_file_id_by_hash(input_hash: str, file_id: str) -> None:
    _require_ready()
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET output_file_id = $1 WHERE input_hash = $2 AND status = 'success'",
                file_id,
                input_hash,
            )
        return

    await _sqlite.execute(
        "UPDATE jobs SET output_file_id = ? WHERE input_hash = ? AND status = 'success'",
        (file_id, input_hash),
    )
    await _sqlite.commit()


async def get_history(user_id: int, limit: int = 10) -> list[dict]:
    _require_ready()
    limit = max(1, min(int(limit), 50))
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jobs WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
                user_id,
                limit,
            )
            return [dict(row) for row in rows]

    cursor = await _sqlite.execute(
        "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Pending uploads (replaces per-process user_data on serverless)
# ---------------------------------------------------------------------------


async def save_pending_file(
    token: str,
    user_id: int,
    file_id: str,
    file_name: str,
    file_size: int,
) -> None:
    _require_ready()
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM pending_files WHERE created_at < NOW() - INTERVAL '2 days'"
            )
            await conn.execute(
                """
                INSERT INTO pending_files (token, user_id, file_id, file_name, file_size)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (token) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    file_id = EXCLUDED.file_id,
                    file_name = EXCLUDED.file_name,
                    file_size = EXCLUDED.file_size,
                    created_at = NOW()
                """,
                token,
                user_id,
                file_id,
                file_name,
                int(file_size or 0),
            )
        return

    await _sqlite.execute(
        "DELETE FROM pending_files WHERE created_at < datetime('now', '-2 days')"
    )
    await _sqlite.execute(
        """
        INSERT OR REPLACE INTO pending_files
            (token, user_id, file_id, file_name, file_size)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token, user_id, file_id, file_name, int(file_size or 0)),
    )
    await _sqlite.commit()


async def get_pending_file(token: str, user_id: int) -> dict | None:
    _require_ready()
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT file_id, file_name, file_size FROM pending_files
                WHERE token = $1 AND user_id = $2
                """,
                token,
                user_id,
            )
            return dict(row) if row else None

    cursor = await _sqlite.execute(
        """
        SELECT file_id, file_name, file_size FROM pending_files
        WHERE token = ? AND user_id = ?
        """,
        (token, user_id),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def claim_pending_file(token: str, user_id: int) -> dict | None:
    """Atomically read and delete a pending file token.

    Webhook deliveries can be retried by Telegram when a conversion takes
    longer than the webhook timeout.  Deleting the token in the same statement
    that reads it guarantees that only one of those deliveries starts the
    conversion, even when they land on different serverless instances.
    """
    _require_ready()
    if _is_postgres:
        async with _pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM pending_files
                WHERE token = $1 AND user_id = $2
                RETURNING file_id, file_name, file_size
                """,
                token,
                user_id,
            )
            return dict(row) if row else None

    cursor = await _sqlite.execute(
        """
        DELETE FROM pending_files
        WHERE token = ? AND user_id = ?
        RETURNING file_id, file_name, file_size
        """,
        (token, user_id),
    )
    row = await cursor.fetchone()
    await _sqlite.commit()
    return dict(row) if row else None


async def close() -> None:
    global _pg_pool, _sqlite
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None
        logger.info("Postgres pool closed")
    if _sqlite is not None:
        await _sqlite.close()
        _sqlite = None
        logger.info("SQLite connection closed")
