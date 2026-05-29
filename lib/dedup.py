"""Content-hash based deduplication.

A finding/event is identified by the SHA-256 hash of its normalized
(url + title). The same paper or event seen across any of the 21 weekly
sweeps hashes to the same id and is skipped.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.data_store import DataStore


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace so trivial variations hash identically."""
    return " ".join((text or "").split()).lower()


class Deduplicator:
    """Content-hash based deduplication backed by the seen_hashes table.

    Borrows the connection from the shared DataStore so dedup state and the
    findings/events tables live in one database.
    """

    def __init__(self, store: "DataStore"):
        self.store = store

    def compute_hash(self, url: str, title: str) -> str:
        """SHA-256 hash of normalized (url + title). This is the finding/event ID."""
        normalized = _normalize(url) + "|" + _normalize(title)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def is_seen(self, hash: str) -> bool:
        """Check if this hash exists in the seen_hashes table."""
        conn = self.store.connection
        async with conn.execute(
            "SELECT 1 FROM seen_hashes WHERE hash = ? LIMIT 1", (hash,)
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def mark_seen(self, hash: str, source: str) -> None:
        """Add a hash to the seen_hashes table (idempotent)."""
        conn = self.store.connection
        await conn.execute(
            "INSERT OR IGNORE INTO seen_hashes (hash, source) VALUES (?, ?)",
            (hash, source),
        )
        await conn.commit()

    async def cleanup(self, days: int = 90) -> int:
        """Remove hashes older than N days. Returns the count removed."""
        conn = self.store.connection
        cursor = await conn.execute(
            "DELETE FROM seen_hashes WHERE first_seen < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        await conn.commit()
        return cursor.rowcount
