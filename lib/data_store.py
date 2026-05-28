"""Database operations for the findings and events tables.

Async SQLite (aiosqlite) backing the shared data store that Systems 1, 2, and 3
communicate through. The same schema runs on SQLite (dev) and Postgres (prod);
only dialect-agnostic SQL is used here.
"""

from __future__ import annotations

import json
import os

import aiosqlite

# Full schema — identical for SQLite and PostgreSQL.
SCHEMA_SQL = """
-- Findings from System 1 (research monitoring)
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,                    -- SHA-256 hash of (source_url + title)
    system TEXT NOT NULL DEFAULT 'research',
    focus_area TEXT NOT NULL,               -- power_generation | energy_storage | power_electronics | semiconductors | deep_tech_infra
    agent TEXT NOT NULL,                    -- e.g. "power_generation_agent"
    title TEXT NOT NULL,
    summary TEXT,                           -- LLM-generated 2-3 sentence summary
    relevance_score REAL,                   -- 0-10, from LLM scoring
    researchers TEXT,                       -- JSON array of names
    affiliation TEXT,
    contact_info TEXT,
    source_url TEXT NOT NULL,
    source_type TEXT,                       -- preprint | journal | news | patent | lab_page | startup
    trl_estimate TEXT,                      -- e.g. "TRL 3-4"
    raw_content TEXT,                       -- original scraped text (for dedup and re-scoring)
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reported BOOLEAN NOT NULL DEFAULT FALSE,
    report_date TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Events from System 2 (event tracking)
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,                    -- SHA-256 hash of (event_name + date + venue)
    system TEXT NOT NULL DEFAULT 'events',
    city TEXT NOT NULL,                     -- boston | nyc | sf
    agent TEXT NOT NULL,                    -- e.g. "boston_events_agent"
    event_name TEXT NOT NULL,
    event_date DATE,
    event_time TEXT,                        -- e.g. "18:00-20:00"
    venue TEXT,
    url TEXT NOT NULL,
    description TEXT,
    cost TEXT,                              -- "Free" | "$50" | "TBD"
    event_type TEXT,                        -- conference | seminar | meetup | workshop | demo_day
    relevance_tags TEXT,                    -- JSON array of focus area tags
    relevance_score REAL,                   -- 0-10
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reported BOOLEAN NOT NULL DEFAULT FALSE,
    report_date TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Content hashes for deduplication
CREATE TABLE IF NOT EXISTS seen_hashes (
    hash TEXT PRIMARY KEY,
    source TEXT NOT NULL,                   -- source name from YAML config
    first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Report log
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    findings_count INTEGER,
    events_count INTEGER,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent',    -- sent | failed
    error_message TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_findings_reported ON findings(reported);
CREATE INDEX IF NOT EXISTS idx_findings_focus_area ON findings(focus_area);
CREATE INDEX IF NOT EXISTS idx_findings_score ON findings(relevance_score);
CREATE INDEX IF NOT EXISTS idx_events_reported ON events(reported);
CREATE INDEX IF NOT EXISTS idx_events_city ON events(city);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
"""

# Columns we accept on insert. Anything else in a passed dict is ignored, and
# any column omitted falls back to its schema default.
FINDING_COLUMNS = (
    "id", "system", "focus_area", "agent", "title", "summary", "relevance_score",
    "researchers", "affiliation", "contact_info", "source_url", "source_type",
    "trl_estimate", "raw_content", "discovered_at", "reported", "report_date",
    "created_at",
)
EVENT_COLUMNS = (
    "id", "system", "city", "agent", "event_name", "event_date", "event_time",
    "venue", "url", "description", "cost", "event_type", "relevance_tags",
    "relevance_score", "discovered_at", "reported", "report_date", "created_at",
)

# Fields stored as JSON text but exposed to callers as Python lists.
_JSON_FIELDS = {"researchers", "relevance_tags"}


def db_path_from_url(url: str) -> str:
    """Convert a ``DATABASE_URL`` into a filesystem path for aiosqlite.

    ``sqlite:///data/lab2scale.db`` -> ``data/lab2scale.db`` (relative)
    ``sqlite:////var/db/x.db``       -> ``/var/db/x.db``       (absolute)
    Non-sqlite URLs are returned unchanged.
    """
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return url[len(prefix):]
    return url


class DataStore:
    """Read/write operations for the findings and events tables.

    Owns a single long-lived aiosqlite connection so that an in-memory
    database (``:memory:``) survives across calls. Use as an async context
    manager, or call ``connect()`` / ``close()`` explicitly.
    """

    def __init__(self, db_path: str = "data/lab2scale.db"):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> "DataStore":
        if self._conn is None:
            if self.db_path != ":memory:":
                parent = os.path.dirname(self.db_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "DataStore":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        """The live connection. Borrowed by the Deduplicator for seen_hashes."""
        if self._conn is None:
            raise RuntimeError("DataStore is not connected. Call connect() first.")
        return self._conn

    async def _ensure(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.connect()
        return self._conn  # type: ignore[return-value]

    async def init_db(self) -> None:
        """Create all tables and indexes. Idempotent."""
        conn = await self._ensure()
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()

    async def _insert(self, table: str, columns: tuple[str, ...], row: dict) -> bool:
        """Insert a row, JSON-encoding list/dict fields. Returns False on duplicate id."""
        present = [c for c in columns if c in row]
        if "id" not in present:
            raise ValueError(f"{table} row requires an 'id' (content hash)")

        values = []
        for col in present:
            val = row[col]
            if col in _JSON_FIELDS and isinstance(val, (list, dict)):
                val = json.dumps(val)
            values.append(val)

        placeholders = ", ".join("?" for _ in present)
        col_list = ", ".join(present)
        sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"

        conn = await self._ensure()
        cursor = await conn.execute(sql, values)
        await conn.commit()
        return cursor.rowcount > 0

    def _row_to_dict(self, row: aiosqlite.Row) -> dict:
        result = dict(row)
        for field in _JSON_FIELDS:
            if field in result and result[field] is not None:
                try:
                    result[field] = json.loads(result[field])
                except (json.JSONDecodeError, TypeError):
                    pass
        if "reported" in result and result["reported"] is not None:
            result["reported"] = bool(result["reported"])
        return result

    async def save_finding(self, finding: dict) -> bool:
        """Insert a finding. Returns False if duplicate (id already present)."""
        return await self._insert("findings", FINDING_COLUMNS, finding)

    async def save_event(self, event: dict) -> bool:
        """Insert an event. Returns False if duplicate."""
        return await self._insert("events", EVENT_COLUMNS, event)

    async def get_unreported_findings(self) -> list[dict]:
        """All findings where reported=False, ordered by relevance_score DESC."""
        conn = await self._ensure()
        async with conn.execute(
            "SELECT * FROM findings WHERE reported = 0 "
            "ORDER BY relevance_score DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_unreported_events(self) -> list[dict]:
        """All events where reported=False, ordered by event_date ASC."""
        conn = await self._ensure()
        async with conn.execute(
            "SELECT * FROM events WHERE reported = 0 ORDER BY event_date ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def mark_reported(self, ids: list[str], table: str) -> None:
        """Set reported=True and report_date=now() for the given IDs."""
        if table not in ("findings", "events"):
            raise ValueError(f"Unknown table: {table!r}")
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        sql = (
            f"UPDATE {table} SET reported = 1, report_date = CURRENT_TIMESTAMP "
            f"WHERE id IN ({placeholders})"
        )
        conn = await self._ensure()
        await conn.execute(sql, list(ids))
        await conn.commit()

    async def log_report(
        self,
        findings_count: int,
        events_count: int,
        recipient: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Insert a record into the reports table."""
        conn = await self._ensure()
        await conn.execute(
            "INSERT INTO reports (findings_count, events_count, recipient, status, "
            "error_message) VALUES (?, ?, ?, ?, ?)",
            (findings_count, events_count, recipient, status, error_message),
        )
        await conn.commit()
