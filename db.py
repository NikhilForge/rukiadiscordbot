"""Persistent storage: Supabase Postgres (asyncpg) with a SQLite fallback.

Both backends implement the same ``Database`` interface, so the rest of the
code does not care which one is active. All SQL is parameterized.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path

import config

log = logging.getLogger("rukia.db")

SQLITE_PATH = Path(__file__).resolve().parent / "rukia.db"


class Database(ABC):
    """Common interface for both storage backends."""

    backend: str = "unknown"

    @abstractmethod
    async def init(self) -> None:
        """Connect and create tables if they do not exist."""

    @abstractmethod
    async def close(self) -> None:
        """Release connections."""

    @abstractmethod
    async def add_messages(self, channel_id: int, items: list[tuple[str, str]]) -> None:
        """Store (role, content) pairs, then prune old rows for the channel."""

    @abstractmethod
    async def get_messages(self, channel_id: int, limit: int) -> list[tuple[str, str]]:
        """Return the last ``limit`` (role, content) pairs, oldest first."""

    @abstractmethod
    async def clear_messages(self, channel_id: int) -> None:
        """Delete all stored history of a channel."""

    @abstractmethod
    async def add_note(self, user_id: int, note: str) -> bool:
        """Save a note. Returns False when the user already has MAX_NOTES."""

    @abstractmethod
    async def get_notes(self, user_id: int) -> list[str]:
        """Return the user's notes, oldest first."""

    @abstractmethod
    async def delete_note(self, user_id: int, number: int) -> bool:
        """Delete note ``number`` (1-based). Returns False if it does not exist."""

    @abstractmethod
    async def clear_notes(self, user_id: int) -> int:
        """Delete all notes of a user. Returns how many were removed."""


# --------------------------------------------------------------------------
# Postgres (Supabase)
# --------------------------------------------------------------------------
_PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS messages (
        id         BIGSERIAL PRIMARY KEY,
        channel_id BIGINT NOT NULL,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_channel_created ON messages (channel_id, created_at)",
    """CREATE TABLE IF NOT EXISTS user_notes (
        id         BIGSERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        note       TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_notes_user ON user_notes (user_id, id)",
    # Row Level Security with no policies: blocks Supabase's public REST API
    # (anon key). The bot's direct database connection is unaffected.
    "ALTER TABLE messages ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE user_notes ENABLE ROW LEVEL SECURITY",
]


class PostgresDatabase(Database):
    backend = "supabase"

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None  # asyncpg.Pool

    async def init(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=5,
            statement_cache_size=0,  # required for transaction-mode poolers
            timeout=15,  # connect timeout
            command_timeout=30,
            max_inactive_connection_lifetime=300,
        )
        async with self._pool.acquire() as conn:
            for statement in _PG_SCHEMA:
                await conn.execute(statement)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def add_messages(self, channel_id: int, items: list[tuple[str, str]]) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for role, content in items:
                    await conn.execute(
                        "INSERT INTO messages (channel_id, role, content) VALUES ($1, $2, $3)",
                        channel_id, role, content,
                    )
                await conn.execute(
                    "DELETE FROM messages WHERE channel_id = $1 AND id NOT IN ("
                    " SELECT id FROM messages WHERE channel_id = $1"
                    " ORDER BY id DESC LIMIT $2)",
                    channel_id, config.DB_HISTORY_LIMIT,
                )

    async def get_messages(self, channel_id: int, limit: int) -> list[tuple[str, str]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM messages WHERE channel_id = $1"
                " ORDER BY created_at DESC, id DESC LIMIT $2",
                channel_id, limit,
            )
        return [(r["role"], r["content"]) for r in reversed(rows)]

    async def clear_messages(self, channel_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM messages WHERE channel_id = $1", channel_id)

    async def add_note(self, user_id: int, note: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_notes WHERE user_id = $1", user_id
                )
                if count >= config.MAX_NOTES:
                    return False
                await conn.execute(
                    "INSERT INTO user_notes (user_id, note) VALUES ($1, $2)", user_id, note
                )
        return True

    async def get_notes(self, user_id: int) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT note FROM user_notes WHERE user_id = $1 ORDER BY id", user_id
            )
        return [r["note"] for r in rows]

    async def delete_note(self, user_id: int, number: int) -> bool:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id FROM user_notes WHERE user_id = $1 ORDER BY id", user_id
            )
            if number < 1 or number > len(rows):
                return False
            await conn.execute(
                "DELETE FROM user_notes WHERE id = $1 AND user_id = $2",
                rows[number - 1]["id"], user_id,
            )
        return True

    async def clear_notes(self, user_id: int) -> int:
        async with self._pool.acquire() as conn:
            status = await conn.execute("DELETE FROM user_notes WHERE user_id = $1", user_id)
        # asyncpg returns a status string such as "DELETE 3"
        try:
            return int(status.split()[-1])
        except (ValueError, IndexError):
            return 0


# --------------------------------------------------------------------------
# SQLite fallback
# --------------------------------------------------------------------------
_SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_messages_channel_created ON messages (channel_id, created_at)",
    """CREATE TABLE IF NOT EXISTS user_notes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        note       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_notes_user ON user_notes (user_id, id)",
]


class SQLiteDatabase(Database):
    backend = "sqlite"

    def __init__(self, path: Path = SQLITE_PATH) -> None:
        self._path = path
        self._conn = None  # aiosqlite.Connection
        self._lock = asyncio.Lock()  # keeps multi-statement writes from interleaving

    async def init(self) -> None:
        import aiosqlite

        self._conn = await aiosqlite.connect(self._path)
        for statement in _SQLITE_SCHEMA:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def add_messages(self, channel_id: int, items: list[tuple[str, str]]) -> None:
        async with self._lock:
            for role, content in items:
                await self._conn.execute(
                    "INSERT INTO messages (channel_id, role, content) VALUES (?, ?, ?)",
                    (channel_id, role, content),
                )
            await self._conn.execute(
                "DELETE FROM messages WHERE channel_id = ? AND id NOT IN ("
                " SELECT id FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?)",
                (channel_id, channel_id, config.DB_HISTORY_LIMIT),
            )
            await self._conn.commit()

    async def get_messages(self, channel_id: int, limit: int) -> list[tuple[str, str]]:
        async with self._conn.execute(
            "SELECT role, content FROM messages WHERE channel_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (channel_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    async def clear_messages(self, channel_id: int) -> None:
        async with self._lock:
            await self._conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
            await self._conn.commit()

    async def add_note(self, user_id: int, note: str) -> bool:
        async with self._lock:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM user_notes WHERE user_id = ?", (user_id,)
            ) as cursor:
                (count,) = await cursor.fetchone()
            if count >= config.MAX_NOTES:
                return False
            await self._conn.execute(
                "INSERT INTO user_notes (user_id, note) VALUES (?, ?)", (user_id, note)
            )
            await self._conn.commit()
        return True

    async def get_notes(self, user_id: int) -> list[str]:
        async with self._conn.execute(
            "SELECT note FROM user_notes WHERE user_id = ? ORDER BY id", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def delete_note(self, user_id: int, number: int) -> bool:
        async with self._lock:
            async with self._conn.execute(
                "SELECT id FROM user_notes WHERE user_id = ? ORDER BY id", (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
            if number < 1 or number > len(rows):
                return False
            await self._conn.execute(
                "DELETE FROM user_notes WHERE id = ? AND user_id = ?",
                (rows[number - 1][0], user_id),
            )
            await self._conn.commit()
        return True

    async def clear_notes(self, user_id: int) -> int:
        async with self._lock:
            cursor = await self._conn.execute(
                "DELETE FROM user_notes WHERE user_id = ?", (user_id,)
            )
            await self._conn.commit()
            return cursor.rowcount or 0


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------
async def create_database(url: str = config.DATABASE_URL) -> Database | None:
    """Return a ready database: Supabase if configured, else SQLite.

    Never raises. Returns None only if even SQLite fails (the bot then keeps
    history in RAM only).
    """
    if url:
        pg = PostgresDatabase(url)
        try:
            await pg.init()
            log.info("Connected to Supabase Postgres. History and notes are persistent.")
            return pg
        except Exception as exc:  # noqa: BLE001 - any failure means "fall back"
            log.error(
                "Could not connect to Supabase (%s: %s). Check DATABASE_URL (use the "
                "Session pooler string). Falling back to local SQLite.",
                type(exc).__name__, exc,
            )
    else:
        log.warning("DATABASE_URL is not set. Using local SQLite file '%s'.", SQLITE_PATH.name)

    lite = SQLiteDatabase()
    try:
        await lite.init()
        log.warning(
            "Using SQLite (%s). History will be LOST on hosts with an ephemeral filesystem.",
            SQLITE_PATH.name,
        )
        return lite
    except Exception as exc:  # noqa: BLE001
        log.error("SQLite failed too (%s). Running with RAM-only history.", exc)
        return None
