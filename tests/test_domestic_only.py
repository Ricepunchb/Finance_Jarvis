import asyncio
import time

import aiosqlite
import pytest

from core import daily_prices, db, reconciliation
from core.config import settings


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    await db._migrate_add_missing_columns(conn)
    return conn


def _forbidden(name):
    async def fn(*args, **kwargs):
        raise AssertionError(f"국내 전용 모드에서 해외 KIS 호출이 발생함: {name}")
    return fn


@pytest.fixture(autouse=True)
def domestic_only(monkeypatch):
    monkeypatch.setattr(settings, "DOMESTIC_ONLY", True)
    monkeypatch.setattr(reconciliation.kis_overseas, "get_ccnl", _forbidden("get_ccnl"))
    monkeypatch.setattr(reconciliation.kis_overseas, "get_present_balance_krw", _forbidden("get_present_balance_krw"))
    monkeypatch.setattr(daily_prices.kis_overseas, "get_daily_chart", _forbidden("get_daily_chart"))


async def _intent(conn, iid, symbol, market):
    now = time.time() - 600
    await conn.execute(
        "INSERT INTO order_intents(intent_id, cycle_id, symbol, market, side, qty, order_type, price, status, client_match_key, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'buy', 1, 'limit', 100, 'SUBMITTED', 'k', ?, ?)", (iid, hash(iid) % 1000, symbol, market, now, now))
    await conn.commit()


def test_reconcile_unresolved_intents_leaves_overseas_intents_untouched(monkeypatch):
    async def run():
        conn = await _conn()
        await _intent(conn, "dom", "005930", "domestic")
        await _intent(conn, "ovs", "AAPL", "overseas")

        async def no_rows(client, start_date, end_date):
            return []
        monkeypatch.setattr(reconciliation.kis_domestic, "get_daily_ccld", no_rows)
        await reconciliation.reconcile_unresolved_intents(object(), conn)
        status = {r["intent_id"]: r["status"] for r in await (await conn.execute("SELECT intent_id, status FROM order_intents")).fetchall()}
        # 국내는 KIS 기록에 없어 NOT_SUBMITTED, 해외는 조회하지 않았으므로 그대로(오판해서 바꾸지 않는다)
        assert status == {"dom": "NOT_SUBMITTED", "ovs": "SUBMITTED"}
        await conn.close()
    asyncio.run(run())


def test_reconcile_positions_syncs_domestic_only_and_keeps_overseas_rows(monkeypatch):
    async def run():
        conn = await _conn()
        await db.sync_positions_from_balance(conn, {"AAPL": (5.0, 100.0, "USD")}, overseas=True)

        async def balance(client):
            return {"holdings": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "270000"}], "summary": {}}
        monkeypatch.setattr(reconciliation.kis_domestic, "get_balance", balance)
        await reconciliation.reconcile_positions(object(), conn)
        assert (await db.get_position(conn, "005930"))["qty"] == 3
        assert (await db.get_position(conn, "AAPL"))["qty"] == 5  # 해외 보유 기록은 지우지 않는다
        await conn.close()
    asyncio.run(run())


def test_ensure_daily_bars_never_calls_kis_for_overseas_but_keeps_cached_history():
    async def run():
        conn = await _conn()
        daily_prices._last_fetch.clear()
        await db.save_daily_bars(conn, "AAPL", "overseas", [{"date": "2026-09-29", "close": 200.0}])
        out = await daily_prices.ensure_daily_bars(
            conn, object(), {"AAPL": {"market": "overseas", "exchange": "NASD"}}, "2026-09-20")
        assert out["AAPL"] == "stale"   # KIS를 부르지 않았고 기존 캐시가 남아 있다
        assert (await db.get_daily_bars(conn, "AAPL", "overseas", "2026-09-20"))[0]["close"] == 200.0
        await conn.close()
    asyncio.run(run())
