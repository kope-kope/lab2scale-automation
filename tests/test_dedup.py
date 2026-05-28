"""Unit tests for lib/dedup.py using in-memory SQLite."""

import asyncio

from lib.data_store import DataStore
from lib.dedup import Deduplicator


async def _fresh() -> tuple[DataStore, Deduplicator]:
    store = DataStore(":memory:")
    await store.init_db()
    return store, Deduplicator(store)


def test_compute_hash_is_deterministic_and_sha256():
    dedup = Deduplicator(DataStore(":memory:"))
    h1 = dedup.compute_hash("https://arxiv.org/abs/1", "A Title")
    h2 = dedup.compute_hash("https://arxiv.org/abs/1", "A Title")
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_compute_hash_normalizes_case_and_whitespace():
    dedup = Deduplicator(DataStore(":memory:"))
    a = dedup.compute_hash("https://arxiv.org/abs/1", "Solid State Battery")
    b = dedup.compute_hash("  https://arxiv.org/abs/1 ", "solid   state   battery")
    assert a == b


def test_compute_hash_differs_on_different_input():
    dedup = Deduplicator(DataStore(":memory:"))
    a = dedup.compute_hash("https://arxiv.org/abs/1", "Title")
    b = dedup.compute_hash("https://arxiv.org/abs/2", "Title")
    c = dedup.compute_hash("https://arxiv.org/abs/1", "Other")
    assert a != b
    assert a != c


def test_is_seen_false_then_true_after_mark():
    async def body():
        store, dedup = await _fresh()
        h = dedup.compute_hash("https://x.com/a", "Paper")
        before = await dedup.is_seen(h)
        await dedup.mark_seen(h, "arxiv_cond_mat")
        after = await dedup.is_seen(h)
        await store.close()
        return before, after

    before, after = asyncio.run(body())
    assert before is False
    assert after is True


def test_mark_seen_is_idempotent():
    async def body():
        store, dedup = await _fresh()
        h = dedup.compute_hash("https://x.com/a", "Paper")
        await dedup.mark_seen(h, "src")
        await dedup.mark_seen(h, "src")  # must not raise on duplicate
        async with store.connection.execute(
            "SELECT COUNT(*) FROM seen_hashes WHERE hash = ?", (h,)
        ) as cur:
            count = (await cur.fetchone())[0]
        await store.close()
        return count

    assert asyncio.run(body()) == 1


def test_cleanup_removes_only_old_hashes():
    async def body():
        store, dedup = await _fresh()
        # Old hash inserted with a backdated first_seen.
        await store.connection.execute(
            "INSERT INTO seen_hashes (hash, source, first_seen) "
            "VALUES (?, ?, datetime('now', '-200 days'))",
            ("old-hash", "src"),
        )
        await store.connection.commit()
        await dedup.mark_seen("recent-hash", "src")

        removed = await dedup.cleanup(days=90)
        old_seen = await dedup.is_seen("old-hash")
        recent_seen = await dedup.is_seen("recent-hash")
        await store.close()
        return removed, old_seen, recent_seen

    removed, old_seen, recent_seen = asyncio.run(body())
    assert removed == 1
    assert old_seen is False
    assert recent_seen is True
