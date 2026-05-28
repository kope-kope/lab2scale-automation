"""Unit tests for lib/data_store.py using in-memory SQLite."""

import asyncio

import pytest

from lib.data_store import DataStore, db_path_from_url


def _finding(**overrides) -> dict:
    base = {
        "id": "finding-1",
        "focus_area": "energy_storage",
        "agent": "energy_storage_agent",
        "title": "Solid-state battery hits 1000 cycles",
        "summary": "Researchers demonstrated a durable solid-state cell.",
        "relevance_score": 8.5,
        "researchers": ["Dr. Jane Smith", "Prof. John Doe"],
        "affiliation": "MIT",
        "contact_info": "jsmith@mit.edu",
        "source_url": "https://arxiv.org/abs/2026.0001",
        "source_type": "preprint",
        "trl_estimate": "TRL 3-4",
        "raw_content": "raw text",
    }
    base.update(overrides)
    return base


def _event(**overrides) -> dict:
    base = {
        "id": "event-1",
        "city": "boston",
        "agent": "boston_events_agent",
        "event_name": "MIT Energy Night",
        "event_date": "2026-06-15",
        "event_time": "18:00-20:00",
        "venue": "MIT Media Lab",
        "url": "https://example.com/event",
        "description": "Panel on power electronics.",
        "cost": "Free",
        "event_type": "seminar",
        "relevance_tags": ["power_electronics", "semiconductors"],
        "relevance_score": 7.0,
    }
    base.update(overrides)
    return base


async def _fresh() -> DataStore:
    store = DataStore(":memory:")
    await store.init_db()
    return store


def test_db_path_from_url():
    assert db_path_from_url("sqlite:///data/lab2scale.db") == "data/lab2scale.db"
    assert db_path_from_url("sqlite:////abs/x.db") == "/abs/x.db"
    assert db_path_from_url("data/raw.db") == "data/raw.db"


def test_init_db_creates_tables():
    async def body():
        store = await _fresh()
        async with store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            names = {r[0] for r in await cur.fetchall()}
        await store.close()
        return names

    names = asyncio.run(body())
    assert {"findings", "events", "seen_hashes", "reports"} <= names


def test_init_db_is_idempotent():
    async def body():
        store = await _fresh()
        await store.init_db()  # second call should not raise
        await store.close()

    asyncio.run(body())


def test_save_finding_and_read_back():
    async def body():
        store = await _fresh()
        assert await store.save_finding(_finding()) is True
        rows = await store.get_unreported_findings()
        await store.close()
        return rows

    rows = asyncio.run(body())
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "finding-1"
    assert row["system"] == "research"  # schema default
    assert row["researchers"] == ["Dr. Jane Smith", "Prof. John Doe"]  # JSON decoded
    assert row["reported"] is False  # stored 0 -> bool
    assert row["relevance_score"] == 8.5


def test_save_finding_duplicate_returns_false():
    async def body():
        store = await _fresh()
        first = await store.save_finding(_finding())
        second = await store.save_finding(_finding(title="A different title"))
        rows = await store.get_unreported_findings()
        await store.close()
        return first, second, rows

    first, second, rows = asyncio.run(body())
    assert first is True
    assert second is False  # same id -> ignored
    assert len(rows) == 1


def test_finding_requires_id():
    async def body():
        store = await _fresh()
        bad = _finding()
        del bad["id"]
        try:
            await store.save_finding(bad)
            return None
        except ValueError as exc:
            return str(exc)
        finally:
            await store.close()

    msg = asyncio.run(body())
    assert msg is not None and "id" in msg


def test_get_unreported_findings_ordered_by_score_desc():
    async def body():
        store = await _fresh()
        await store.save_finding(_finding(id="a", relevance_score=5.0))
        await store.save_finding(_finding(id="b", relevance_score=9.0))
        await store.save_finding(_finding(id="c", relevance_score=7.0))
        rows = await store.get_unreported_findings()
        await store.close()
        return [r["id"] for r in rows]

    assert asyncio.run(body()) == ["b", "c", "a"]


def test_mark_reported_excludes_from_unreported():
    async def body():
        store = await _fresh()
        await store.save_finding(_finding(id="a"))
        await store.save_finding(_finding(id="b"))
        await store.mark_reported(["a"], "findings")
        rows = await store.get_unreported_findings()
        # confirm report_date was set on the reported row
        async with store.connection.execute(
            "SELECT reported, report_date FROM findings WHERE id='a'"
        ) as cur:
            a = await cur.fetchone()
        await store.close()
        return [r["id"] for r in rows], a

    remaining, a = asyncio.run(body())
    assert remaining == ["b"]
    assert a["reported"] == 1
    assert a["report_date"] is not None


def test_mark_reported_invalid_table_raises():
    async def body():
        store = await _fresh()
        try:
            await store.mark_reported(["a"], "; DROP TABLE findings;")
            return False
        except ValueError:
            return True
        finally:
            await store.close()

    assert asyncio.run(body()) is True


def test_mark_reported_empty_ids_is_noop():
    async def body():
        store = await _fresh()
        await store.mark_reported([], "findings")  # should not raise
        await store.close()

    asyncio.run(body())


def test_save_event_and_order_by_date_asc():
    async def body():
        store = await _fresh()
        await store.save_event(_event(id="e2", event_date="2026-07-01"))
        await store.save_event(_event(id="e1", event_date="2026-06-01"))
        await store.save_event(_event(id="e3", event_date="2026-08-01"))
        rows = await store.get_unreported_events()
        await store.close()
        return rows

    rows = asyncio.run(body())
    assert [r["id"] for r in rows] == ["e1", "e2", "e3"]
    assert rows[0]["relevance_tags"] == ["power_electronics", "semiconductors"]
    assert rows[0]["system"] == "events"


def test_save_event_duplicate_returns_false():
    async def body():
        store = await _fresh()
        first = await store.save_event(_event())
        second = await store.save_event(_event(event_name="changed"))
        await store.close()
        return first, second

    first, second = asyncio.run(body())
    assert first is True
    assert second is False


def test_log_report_inserts_row():
    async def body():
        store = await _fresh()
        await store.log_report(12, 5, "team@lab-2-scale.com", "sent")
        await store.log_report(0, 0, "team@lab-2-scale.com", "failed", "smtp error")
        async with store.connection.execute(
            "SELECT findings_count, events_count, recipient, status, error_message "
            "FROM reports ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        await store.close()
        return [dict(r) for r in rows]

    rows = asyncio.run(body())
    assert len(rows) == 2
    assert rows[0]["findings_count"] == 12
    assert rows[0]["status"] == "sent"
    assert rows[0]["error_message"] is None
    assert rows[1]["status"] == "failed"
    assert rows[1]["error_message"] == "smtp error"


def test_connection_property_requires_connect():
    store = DataStore(":memory:")
    with pytest.raises(RuntimeError):
        _ = store.connection
