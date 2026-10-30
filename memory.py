"""Per-channel conversation memory: RAM cache + database persistence."""
from __future__ import annotations

import asyncio
import logging

import config
from db import Database

log = logging.getLogger("rukia.memory")


class ConversationMemory:
    """Keeps the last ``HISTORY_LIMIT`` messages per channel.

    History is loaded from the database on first use and cached in RAM.
    If the database fails, the bot keeps working with RAM-only history.
    """

    def __init__(self, db: Database | None) -> None:
        self._db = db
        self._cache: dict[int, list[dict[str, str]]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, channel_id: int) -> asyncio.Lock:
        """Per-channel lock so simultaneous questions cannot corrupt history."""
        return self._locks.setdefault(channel_id, asyncio.Lock())

    async def history(self, channel_id: int) -> list[dict[str, str]]:
        """Return the cached history (loading it from the database if needed)."""
        if channel_id not in self._cache:
            loaded: list[dict[str, str]] = []
            if self._db is not None:
                try:
                    rows = await self._db.get_messages(channel_id, config.HISTORY_LIMIT)
                    loaded = [{"role": role, "content": content} for role, content in rows]
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not load history for %s (%s). Using RAM only.", channel_id, exc)
            self._cache[channel_id] = loaded
        return list(self._cache[channel_id])

    async def add_exchange(self, channel_id: int, user_text: str, assistant_text: str) -> None:
        """Store one successful user message + assistant reply."""
        cache = self._cache.setdefault(channel_id, [])
        cache.append({"role": "user", "content": user_text})
        cache.append({"role": "assistant", "content": assistant_text})
        del cache[: max(0, len(cache) - config.HISTORY_LIMIT)]

        if self._db is not None:
            try:
                await self._db.add_messages(
                    channel_id, [("user", user_text), ("assistant", assistant_text)]
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not save history for %s (%s). Kept in RAM only.", channel_id, exc)

    async def reset(self, channel_id: int) -> None:
        """Forget a channel's history (RAM and database)."""
        async with self.lock(channel_id):
            self._cache[channel_id] = []
            if self._db is not None:
                try:
                    await self._db.clear_messages(channel_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not clear stored history for %s (%s).", channel_id, exc)
