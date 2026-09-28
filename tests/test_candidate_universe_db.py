import asyncio
import json
import time

import aiosqlite

from core import db


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    return conn


async def _enabled(conn) -> dict:
    return {row["symbol"]: row for row in await db.list_candidate_universe(conn)}


def test_dynamic_upsert_does_not_demote_seed_candidate():
    async def run():
        conn = await _conn()
        await db.add_candidate_symbol(conn, "005930", "삼성전자", "KOSPI_LARGE_CAP")
        await db.upsert_dynamic_candidate(conn, "005930", "삼성전자", json.dumps({"broker": {}}), time.time() - 1)
        assert await db.disable_expired_candidates(conn, time.time()) == 0
        row = (await _enabled(conn))["005930"]
        assert row["universe_tag"] == "KOSPI_LARGE_CAP" and row["expires_at"] is None
        assert json.loads(row["sources_json"]) == {"broker": {}}
    asyncio.run(run())


def test_expired_dynamic_candidate_is_disabled_then_revived_on_rehit():
    async def run():
        conn = await _conn()
        await db.upsert_dynamic_candidate(conn, "000720", "현대건설", "{}", time.time() - 1)
        assert await db.disable_expired_candidates(conn, time.time()) == 1
        assert "000720" not in await _enabled(conn)
        await db.upsert_dynamic_candidate(conn, "000720", "현대건설", "{}", time.time() + 86400)
        assert "000720" in await _enabled(conn)
    asyncio.run(run())


def test_user_removal_is_sticky_until_manual_readd():
    async def run():
        conn = await _conn()
        await db.upsert_dynamic_candidate(conn, "353200", "대덕전자", "{}", time.time() + 86400)
        assert await db.disable_candidate_symbol(conn, "353200")
        await db.upsert_dynamic_candidate(conn, "353200", "대덕전자", "{}", time.time() + 86400)
        assert "353200" not in await _enabled(conn)
        await db.add_candidate_symbol(conn, "353200", "대덕전자", "MANUAL_WATCHLIST")
        assert (await _enabled(conn))["353200"]["universe_tag"] == "MANUAL_WATCHLIST"
    asyncio.run(run())


def test_trim_keeps_most_recently_seen_dynamic_and_all_permanent():
    async def run():
        conn = await _conn()
        await db.add_candidate_symbol(conn, "005930", "삼성전자", "KOSPI_LARGE_CAP")
        for i, symbol in enumerate(["A00001", "A00002", "A00003"]):
            await db.upsert_dynamic_candidate(conn, symbol, symbol, "{}", time.time() + 86400)
            await conn.execute("UPDATE candidate_universe SET last_seen_at = ? WHERE symbol = ?", (1000 + i, symbol))
        assert await db.trim_dynamic_candidates(conn, keep=2) == 1
        assert set(await _enabled(conn)) == {"005930", "A00002", "A00003"}
    asyncio.run(run())
