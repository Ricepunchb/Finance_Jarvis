import asyncio
import json
from datetime import datetime, timedelta

import aiosqlite
import pandas as pd

from core import analytics, daily_prices, db, pnl
from core.pnl import KST


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    await db._migrate_add_missing_columns(conn)
    return conn


def _ts(day: str, hour: int = 10) -> float:
    return datetime.strptime(f"{day} {hour:02d}:00", "%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()


def _day(offset: int) -> str:
    return (datetime.now(KST) - timedelta(days=offset)).strftime("%Y-%m-%d")


async def _seed_sell_without_fills(conn):
    """과거 REST 체결 기록 이전의 트레일링 익절: status=FILLED인데 fills가 없다."""
    sell_day, buy_day = _day(5), _day(3)
    await conn.execute("INSERT INTO cycles(cycle_id, started_at) VALUES (1, ?)", (_ts(_day(8)),))
    await conn.execute("INSERT INTO portfolio_symbols(symbol, market, added_at, enabled) VALUES ('000660','domestic',0,1)")
    await conn.execute(
        "INSERT INTO order_intents(intent_id, cycle_id, symbol, market, side, qty, order_type, price, status, client_match_key, reason, created_at, updated_at)"
        " VALUES ('s1',1,'000660','domestic','sell',1,'market',NULL,'FILLED','k1','TRAILING_TAKE_PROFIT',?,?)", (_ts(sell_day), _ts(sell_day)))
    await conn.execute(
        "INSERT INTO order_intents(intent_id, cycle_id, symbol, market, side, qty, order_type, price, status, client_match_key, reason, created_at, updated_at)"
        " VALUES ('b1',2,'000660','domestic','buy',1,'limit',1776000,'FILLED','k2','onboarding_tranche',?,?)", (_ts(buy_day), _ts(buy_day)))
    await conn.execute("INSERT INTO fills(intent_id, qty, price, filled_at, source) VALUES ('b1',1,1776000,?, 'REST_POLL')", (_ts(buy_day),))
    ctx = {"price": 1810000.0, "exit_check": {"avg_price": 1873000.0, "peak_price": 1913000.0, "fired": "TRAILING_TAKE_PROFIT"}, "total_equity": 100_000_000}
    await conn.execute(
        "INSERT INTO decision_log(cycle_id, symbol, ts, action, qty, reason, intent_id, context_json) VALUES (1,'000660',?,'SELL',1,'TRAILING_TAKE_PROFIT','s1',?)",
        (_ts(sell_day), json.dumps(ctx)))
    await conn.execute("INSERT INTO positions(symbol, qty, avg_price, currency, last_synced_at) VALUES ('000660',1,1776000,'KRW',0)")
    for offset, close in ((8, 1_850_000), (6, 1_830_000), (5, 1_810_000), (4, 1_790_000), (3, 1_780_000), (1, 1_800_000), (0, 1_790_000)):
        await conn.execute("INSERT INTO daily_bars(symbol, market, date, close) VALUES ('000660','domestic',?,?)", (_day(offset), close))
    await conn.commit()


def test_overview_estimates_unfilled_sell_and_reconciles_totals():
    async def run():
        conn = await _conn()
        await _seed_sell_without_fills(conn)
        data = await analytics.build_overview(conn, None, days=30, fetch_prices=False)
        row = data["symbols"][0]
        assert row["realized_krw"] == (1_810_000 - 1_873_000) * 1  # exit_check 평단 기준 추정
        assert row["estimated"] and data["kpis"]["realized_krw"] == -63_000
        assert row["qty"] == 1 and row["unrealized_krw"] == (1_790_000 - 1_776_000)
        assert row["total_pnl_krw"] == row["realized_krw"] + row["unrealized_krw"]
        # 일별 평가손익의 누적 합 = 기간 손익 KPI = 종목 기간손익
        assert data["kpis"]["period_pnl_krw"] == row["period_pnl_krw"] == data["daily"][-1]["cum_pnl_krw"]
        # 기록 시작일(첫 사이클)보다 앞선 날짜는 축에 없다
        assert data["daily"][0]["date"] >= data["range"]["data_start"]
        await conn.close()
    asyncio.run(run())


def test_realized_calendar_buckets_by_day_and_marks_estimates():
    async def run():
        conn = await _conn()
        await _seed_sell_without_fills(conn)
        out = await analytics.build_realized(conn, _day(10), _day(0))
        by_date = {d["date"]: d for d in out["days"]}
        assert by_date[_day(5)]["pnl_krw"] == -63_000 and by_date[_day(5)]["estimated"] and by_date[_day(5)]["sells"] == 1
        assert by_date[_day(3)]["buys"] == 1 and by_date[_day(3)]["pnl_krw"] == 0
        sell = next(t for t in out["trades"] if t["side"] == "sell")
        assert sell["pnl_krw"] == -63_000 and sell["estimated"]
        assert out["month_total_krw"] == -63_000
        only_other = await analytics.build_realized(conn, _day(10), _day(0), symbol="005930")
        assert only_other["days"] == []
        await conn.close()
    asyncio.run(run())


def test_decision_list_classifies_markers_and_detail_parses_context():
    async def run():
        conn = await _conn()
        await _seed_sell_without_fills(conn)
        await conn.execute("INSERT INTO decision_log(cycle_id, symbol, ts, action, reason, context_json) VALUES (3,'000660',?,'NO_OP','조건 미충족','{}')", (_ts(_day(2)),))
        await conn.execute(
            "INSERT INTO order_intents(intent_id, cycle_id, symbol, market, side, qty, order_type, price, status, client_match_key, created_at, updated_at)"
            " VALUES ('r1',4,'000660','domestic','buy',1,'limit',1700000,'REJECTED','k3',?,?)", (_ts(_day(2)), _ts(_day(2))))
        await conn.execute("INSERT INTO decision_log(cycle_id, symbol, ts, action, qty, intent_id, context_json) VALUES (4,'000660',?,'BUY',1,'r1','{}')", (_ts(_day(2), 11),))
        await conn.commit()
        rows = await analytics.list_decisions(conn, "000660", _day(9), _day(0), include_noop=True)
        kinds = {(r["action"], r["marker"]) for r in rows}
        assert kinds == {("SELL", "executed"), ("NO_OP", "noop"), ("BUY", "rejected")}
        sell = next(r for r in rows if r["action"] == "SELL")
        assert sell["price"] == 1_810_000.0 and sell["date"] == _day(5)
        assert len(await analytics.list_decisions(conn, None, _day(9), _day(0), include_noop=False)) == 2
        detail = await analytics.get_decision_detail(conn, sell["id"])
        assert detail["context"]["exit_check"]["avg_price"] == 1_873_000.0 and "context_json" not in detail
        assert await analytics.get_decision_detail(conn, 9999) is None
        await conn.close()
    asyncio.run(run())


class _FakeKIS:
    """get_daily_chart를 가로채 호출 횟수를 센다."""
    calls = 0


def test_ensure_daily_bars_fetches_incrementally_and_falls_back_to_intraday(monkeypatch):
    async def run():
        conn = await _conn()
        daily_prices._last_fetch.clear()
        today = datetime.now(KST).date()
        rows = [{"stck_bsop_date": (today - timedelta(days=i)).strftime("%Y%m%d"), "stck_oprc": "1", "stck_hgpr": "1",
                 "stck_lwpr": "1", "stck_clpr": str(100 + i), "acml_vol": "1"} for i in range(3)]

        async def fake_chart(client, symbol, start, end, period="D"):
            _FakeKIS.calls += 1
            return rows
        monkeypatch.setattr(daily_prices.kis_domestic, "get_daily_chart", fake_chart)
        since = (today - timedelta(days=10)).strftime("%Y-%m-%d")
        infos = {"005930": {"market": "domestic", "exchange": None}}

        status = await daily_prices.ensure_daily_bars(conn, object(), infos, since)
        assert status == {"005930": "fetched"} and _FakeKIS.calls >= 1
        stored = await db.get_daily_bars(conn, "005930", "domestic", since)
        assert [b["close"] for b in stored] == [102.0, 101.0, 100.0]

        calls = _FakeKIS.calls
        assert (await daily_prices.ensure_daily_bars(conn, object(), infos, since))["005930"] == "cached"  # TTL 안에서는 재조회 없음
        assert _FakeKIS.calls == calls
        assert (await daily_prices.ensure_daily_bars(conn, object(), infos, since, force=True))["005930"] == "fetched"

        # KIS 클라이언트가 없으면 30분봉 캐시를 일 단위로 접는다
        await db.save_intraday_bars(conn, "AAPL", "overseas", [
            {"bar_start": _ts(_day(2), 23), "open": 1, "high": 1, "low": 1, "close": 10.0, "volume": 1},
            {"bar_start": _ts(_day(2), 23) + 1800, "open": 1, "high": 1, "low": 1, "close": 11.0, "volume": 1}])
        out = await daily_prices.ensure_daily_bars(conn, None, {"AAPL": {"market": "overseas", "exchange": "NASD"}}, _day(5))
        assert out["AAPL"] == "fallback_intraday"
        assert (await db.get_daily_bars(conn, "AAPL", "overseas", _day(5)))[-1]["close"] == 11.0
        none = await daily_prices.ensure_daily_bars(conn, None, {"ZZZ": {"market": "domestic", "exchange": None}}, _day(5))
        assert none["ZZZ"] == "failed"
        await conn.close()
    asyncio.run(run())


def test_overseas_daily_fetch_pages_backwards_until_since(monkeypatch):
    async def run():
        conn = await _conn()
        daily_prices._last_fetch.clear()
        pages = {"": ["20260930", "20260929"], "20260928": ["20260928", "20260925"]}
        seen = []

        async def fake_chart(client, excd, symbol, base_date="", modified_price=True):
            seen.append(base_date)
            return [{"xymd": d, "open": "1", "high": "1", "low": "1", "clos": "10", "tvol": "1"} for d in pages.get(base_date, [])]
        monkeypatch.setattr(daily_prices.kis_overseas, "get_daily_chart", fake_chart)
        bars = await daily_prices._fetch_overseas(object(), "NASD", "TSLA", "2026-09-25")
        assert seen == ["", "20260928"] and sorted(b["date"] for b in bars)[0] == "2026-09-25"
        await conn.close()
    asyncio.run(run())


def test_weighted_return_renormalizes_over_symbols_with_prices():
    closes = {"A": {"2026-09-01": 100.0, "2026-09-10": 110.0}, "B": {"2026-09-01": 200.0, "2026-09-10": 180.0}}
    ret, skipped = analytics.weighted_return({"A": 0.5, "B": 0.5, "C": 0.2}, closes, "2026-09-05")
    assert abs(ret - (0.5 * 0.10 + 0.5 * -0.10) / 1.0) < 1e-12 and skipped == ["C"]
    assert analytics.weighted_return({"A": 0.0}, closes, "2026-09-05") == (None, [])
    # 승인 시점 이전 가격이 없으면 제외
    assert analytics.weighted_return({"A": 1.0}, closes, "2026-08-01") == (None, ["A"])


def test_rebalance_history_builds_timeline_stats_and_post_decision_returns():
    async def run():
        conn = await _conn()
        now = datetime.now(KST).timestamp()
        prior = {"weights": {"A": 0.6, "B": 0.4}, "symbols": ["A", "B"]}
        proposed = {"weights": {"A": 0.3, "B": 0.4, "C": 0.3}, "symbols": ["A", "B", "C"]}
        ctx = json.dumps({"input": {}, "output": {"rationale": "C 편입"}, "rejected_adds": []})

        async def event(status, trig, created, snap):
            await conn.execute(
                "INSERT INTO rebalance_events(trigger_type, autonomy_mode, status, rationale, symbols_added, prior_snapshot_json, proposed_snapshot_json, context_json, created_at, decided_at)"
                " VALUES (?, 'approval_gated', ?, 'r', ?, ?, ?, ?, ?, ?)",
                (trig, status, json.dumps([{"symbol": "C", "name": "씨", "rationale": "x"}]), json.dumps(prior),
                 json.dumps(snap) if snap else None, ctx, created, created + 60 if status in ("APPROVED", "REJECTED") else None))
        await event("APPROVED", "SCHEDULED", now - 5 * 86400, proposed)
        await event("REJECTED", "DRIFT", now - 3 * 86400, proposed)
        await event("BLOCKED", "MANUAL", now - 2 * 86400, None)
        await conn.execute("INSERT INTO active_target_weights(symbol, weight, approved_at, source_proposal_id) VALUES ('A',0.3,0,1)")
        for sym in "ABC":
            await conn.execute("INSERT INTO portfolio_symbols(symbol, market, added_at, enabled) VALUES (?,'domestic',0,1)", (sym,))
        d0, d1 = _day(5), _day(0)
        for sym, (p0, p1) in {"A": (100, 90), "B": (100, 100), "C": (100, 120)}.items():
            for day, close in ((d0, p0), (d1, p1)):
                await conn.execute("INSERT INTO daily_bars(symbol, market, date, close) VALUES (?,'domestic',?,?)", (sym, day, close))
        await conn.commit()

        out = await analytics.build_rebalance_history(conn, None, limit=10, with_returns=True)
        assert out["stats"]["by_status"] == {"APPROVED": 1, "REJECTED": 1, "BLOCKED": 1}
        assert out["stats"]["approval_rate"] == 0.5 and out["stats"]["decided_count"] == 2
        assert [p["label"] for p in out["timeline"]] == ["시작", "#1 SCHEDULED", "현재"]
        assert out["timeline"][0]["weights"] == prior["weights"] and out["timeline"][-1]["weights"] == {"A": 0.3}
        blocked = next(e for e in out["events"] if e["status"] == "BLOCKED")
        assert blocked["proposed_weights"] is None and blocked["prior_weights"] == prior["weights"]
        r = out["returns"][0]  # 승인 이벤트 1건만
        assert len(out["returns"]) == 1 and r["event_id"] == 1
        assert abs(r["prior_return"] - (0.6 * -0.10 + 0.4 * 0.0)) < 1e-9
        assert abs(r["proposed_return"] - (0.3 * -0.10 + 0.4 * 0.0 + 0.3 * 0.2)) < 1e-9
        assert abs(r["excess"] - (r["proposed_return"] - r["prior_return"])) < 1e-12
        await conn.close()
    asyncio.run(run())


def test_overview_uses_positions_as_ground_truth_and_suppresses_overseas_mismatch():
    async def run():
        conn = await _conn()
        now = datetime.now(KST).timestamp()
        # 해외 종목 NFLX 매수 체결이 있으나 현재 positions는 0주
        await conn.execute("INSERT INTO cycles(cycle_id, started_at) VALUES (1, ?)", (now - 86400,))
        await conn.execute("INSERT INTO portfolio_symbols(symbol, market, added_at, enabled) VALUES ('NFLX','overseas',0,0)")
        await conn.execute(
            "INSERT INTO order_intents(intent_id, cycle_id, symbol, market, side, qty, order_type, price, status, client_match_key, created_at, updated_at)"
            " VALUES ('nflx_b',1,'NFLX','overseas','buy',31,'limit',70.0,'FILLED','k_nflx',?,?)", (now - 86400, now - 86400)
        )
        await conn.execute("INSERT INTO fills(intent_id, qty, price, filled_at, source) VALUES ('nflx_b',31,70.0,?,'REST_POLL')", (now - 86400,))
        await conn.execute("INSERT INTO positions(symbol, qty, avg_price, currency, last_synced_at) VALUES ('NFLX',0,0,'USD',?)", (now,))

        # 국내 종목 017670 (SKT) 잔고 17주
        await conn.execute("INSERT INTO portfolio_symbols(symbol, market, added_at, enabled) VALUES ('017670','domestic',0,1)")
        await conn.execute("INSERT INTO positions(symbol, qty, avg_price, currency, last_synced_at) VALUES ('017670',17,85000,'KRW',?)", (now,))

        data = await analytics.build_overview(conn, None, days=30, fetch_prices=False)

        # 1. positions 잔고가 0인 NFLX의 수량(qty)은 0이어야 하고, 017670은 17이어야 한다
        symbols_by_id = {s["symbol"]: s for s in data["symbols"]}
        assert symbols_by_id["NFLX"]["qty"] == 0.0
        assert symbols_by_id["017670"]["qty"] == 17.0

        # 2. DOMESTIC_ONLY 모드이므로 해외 종목(NFLX)의 수량 불일치 경고는 warnings에 나타나지 않아야 한다
        assert not any("잔고(positions)보다 체결 매수" in w and "NFLX" in w for w in data.get("warnings", []))
        assert symbols_by_id["NFLX"]["mismatch_qty"] is None

        await conn.close()
    asyncio.run(run())
