import asyncio

import aiosqlite

from core import db, signal_engine
from core.config import settings
from core.risk import RiskManager


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    await db._migrate_add_missing_columns(conn)
    return conn


def test_liquidated_symbol_resets_entry_state_and_reentry_gets_fresh_peak():
    async def run():
        conn = await _conn()
        await db.sync_positions_from_balance(conn, {"000660": (1, 1_873_000, "KRW")}, overseas=False)
        await db.update_position_peak(conn, "000660", 1_913_000)
        # 전량 매도 -> 잔고에서 종목이 사라짐
        await db.sync_positions_from_balance(conn, {}, overseas=False)
        pos = await db.get_position(conn, "000660")
        assert pos["qty"] == 0 and pos["peak_price_since_entry"] is None and pos["entry_avg_price"] is None
        # 재진입 시 옛 고점이 이월되지 않는다
        await db.sync_positions_from_balance(conn, {"000660": (1, 1_700_000, "KRW")}, overseas=False)
        assert (await db.get_position(conn, "000660"))["peak_price_since_entry"] == 1_700_000
    asyncio.run(run())


def test_domestic_sync_does_not_touch_overseas_positions():
    async def run():
        conn = await _conn()
        await db.sync_positions_from_balance(conn, {"AMD": (3, 100.0, "USD")}, overseas=True)
        await db.sync_positions_from_balance(conn, {}, overseas=False)
        assert (await db.get_position(conn, "AMD"))["qty"] == 3
    asyncio.run(run())


def test_rest_fill_is_incremental_and_idempotent():
    async def run():
        conn = await _conn()
        iid = await db.create_order_intent(conn, 1, "005930", "sell", 4, "market", None, "x")
        await db.record_rest_fill(conn, iid, 4, 272_000)
        await db.record_rest_fill(conn, iid, 4, 272_000)
        cur = await conn.execute("SELECT qty, price, source FROM fills WHERE intent_id = ?", (iid,))
        rows = await cur.fetchall()
        assert len(rows) == 1 and rows[0]["qty"] == 4 and rows[0]["source"] == "REST_POLL"
    asyncio.run(run())


def _decide(target, qty, price=1_800_000, weight=0.0):
    async def run():
        conn = await _conn()
        return await signal_engine.decide(
            current_weight=weight, target_weight=target, band=0.05,
            tech_signal={"direction": "BUY", "strength": 1.0}, intraday_signal={"direction": "BUY", "strength": 1.0},
            sentiment_signal=None, price=price, current_position_qty=qty,
            current_position_value=qty * price, total_equity=352_000_000, risk=RiskManager(conn),
        )
    return asyncio.run(run())


def test_excluded_symbol_is_never_bought_even_on_strong_buy_signal():
    assert _decide(target=0.0, qty=0) is None


def test_excluded_symbol_with_holding_is_fully_liquidated_at_least_one_share():
    action = _decide(target=0.0, qty=1, weight=0.005)
    assert action.side == "sell" and action.qty == 1 and action.reason == "excluded_liquidation"


# --- ATR 트레일링 / 분할 온보딩 ---

import pandas as pd

from core import exit_guard, indicators


def test_atr_pct_needs_enough_bars_and_scales_with_range():
    df = pd.DataFrame({"high": [102] * 20, "low": [98] * 20, "close": [100] * 20})
    assert abs(indicators.compute_atr_pct(df, 14) - 0.04) < 1e-9
    assert indicators.compute_atr_pct(df.head(10), 14) is None


def test_trailing_not_armed_when_peak_barely_above_avg_hynix_case():
    # 어제 하이닉스: 평단 1,873,000 / 고점 1,913,000(+2.1%) / 현재 1,810,000 -> 더 이상 손실 익절 발동 안 함
    assert exit_guard.evaluate_exit(1_873_000, 1_913_000, 1_810_000, atr_pct=0.03) is None
    assert exit_guard.evaluate_exit(1_873_000, 1_913_000, 1_810_000, atr_pct=None) is None


def test_trailing_fires_after_armed_and_wider_for_volatile_symbol():
    # ATR 4% -> 폭 10%, 무장 +6%. 고점 +10%에서 -8% 하락은 아직, -11%면 발동
    assert exit_guard.evaluate_exit(100, 110, 101.2, atr_pct=0.04) is None
    assert exit_guard.evaluate_exit(100, 110, 97.0, atr_pct=0.04).reason == "TRAILING_TAKE_PROFIT"
    # 고정 폴백(5%) 기준이면 같은 하락(-8%)에서 이미 발동
    assert exit_guard.evaluate_exit(100, 110, 101.2, atr_pct=None).reason == "TRAILING_TAKE_PROFIT"


def _decide_onboard(weight, target=0.08, direction="HOLD", strength=0.1, price=100_000, prior_notional=0.0):
    async def run():
        conn = await _conn()
        risk = RiskManager(conn)
        if prior_notional:
            await db.create_order_intent(conn, 1, "005930", "buy", prior_notional / price, "limit", price, "onboarding_tranche")
        sig = {"direction": direction, "strength": strength}
        return await signal_engine.decide(
            symbol="005930", current_weight=weight, target_weight=target, band=0.05,
            tech_signal=sig, intraday_signal=None, sentiment_signal=None, price=price,
            current_position_qty=0, current_position_value=weight * 352_000_000,
            total_equity=352_000_000, risk=risk,
        )
    return asyncio.run(run())


def test_onboarding_buys_underweight_symbol_on_hold_signal_within_order_cap():
    a = _decide_onboard(weight=0.0)
    assert a.side == "buy" and a.reason == "onboarding_tranche" and 0 < a.qty * 100_000 <= 1_000_000


def test_onboarding_skipped_on_sell_signal_in_band_or_when_daily_budget_used():
    assert _decide_onboard(0.0, direction="SELL", strength=0.4) is None
    assert _decide_onboard(0.07) is None                       # 밴드 안
    daily_budget = 0.08 * 352_000_000 / settings.ONBOARDING_DAYS
    assert _decide_onboard(0.0, prior_notional=daily_budget + 100_000) is None  # 일일 예산 소진


def test_onboarding_allows_one_share_when_price_exceeds_order_cap():
    a = _decide_onboard(0.0, price=1_800_000)
    assert a.qty == 1


# --- 포트폴리오 삭제(정리 대기): 즉시 청산이 아니라 매도 신호/기한까지 대기 ---

def _decide_winddown(tech, intraday, liquidating=False, qty=10, price=100_000):
    async def run():
        conn = await _conn()
        return await signal_engine.decide(
            current_weight=0.0, target_weight=0.0, band=0.05,
            tech_signal=tech, intraday_signal=intraday, sentiment_signal=None,
            price=price, current_position_qty=qty, current_position_value=qty * price,
            total_equity=352_000_000, risk=RiskManager(conn),
            winding_down=True, winddown_liquidating=liquidating,
        )
    return asyncio.run(run())


BUY = {"direction": "BUY", "strength": 1.0}
SELL = {"direction": "SELL", "strength": 1.0}
HOLD = {"direction": "HOLD", "strength": 0.0}


def test_winddown_waits_while_signal_is_not_sell():
    assert _decide_winddown(BUY, BUY) is None
    assert _decide_winddown(HOLD, HOLD) is None


def test_winddown_sells_all_on_sell_signal():
    action = _decide_winddown(SELL, SELL, qty=10)
    assert action.side == "sell" and action.qty == 10 and action.reason == "winddown_signal_sell"


def test_winddown_liquidating_sells_even_on_buy_signal():
    action = _decide_winddown(BUY, BUY, liquidating=True, qty=10)
    assert action.side == "sell" and action.qty == 10 and action.reason == "winddown_liquidation"


def test_winddown_never_buys_and_ignores_empty_position():
    assert _decide_winddown(BUY, BUY, liquidating=True, qty=0) is None
    assert _decide_winddown(SELL, SELL, qty=0) is None


def test_winddown_lifecycle_in_db():
    async def run():
        conn = await _conn()
        await db.add_portfolio_symbol(conn, "005930")
        assert await db.start_symbol_winddown(conn, "005930")
        started = (await db.get_portfolio_symbol_info(conn))["005930"]["winding_down_at"]
        assert started is not None
        await db.start_symbol_winddown(conn, "005930")  # 재삭제해도 기한이 연장되지 않는다
        assert (await db.get_portfolio_symbol_info(conn))["005930"]["winding_down_at"] == started

        await db.mark_winddown_sell_started(conn, "005930")
        assert (await db.get_portfolio_symbol_info(conn))["005930"]["winddown_sell_started_at"] is not None

        pid = await db.propose_target_weight(conn, "005930", 0.0, proposed_by="manual")
        await db.decide_target_weight(conn, pid, approve=True)
        await db.finish_symbol_removal(conn, "005930")
        assert await db.list_portfolio_symbols(conn) == []
        assert "005930" not in await db.get_active_target_weights(conn)

        # 다시 등록하면 정리 상태가 초기화된다
        await db.add_portfolio_symbol(conn, "005930")
        info = (await db.get_portfolio_symbol_info(conn))["005930"]
        assert info["winding_down_at"] is None and info["winddown_sell_started_at"] is None
    asyncio.run(run())


def test_on_fill_triggers_reconciliation_double_check():
    """웹소켓 체결통보(on_fill) 수신 시 KIS 잔고 재조회(reconcile_positions)를 호출하여 positions를 동기화하는지 검증."""
    from unittest.mock import AsyncMock, patch
    from core.engine import TradingEngine

    async def run():
        conn = await _conn()
        engine = TradingEngine()
        engine.conn = conn

        # 가상 intent 생성
        iid = await db.create_order_intent(conn, 1, "005930", "sell", 4, "limit", 270000, "test")
        await db.update_order_intent(conn, iid, status="SUBMITTED", kis_order_no="99999")

        # reconcile_positions 모킹
        with patch("core.reconciliation.reconcile_positions", new_callable=AsyncMock) as mock_reconcile:
            msg = {"ODER_NO": "99999", "CNTG_YN": "2", "CNTG_QTY": "4", "CNTG_UNPR": "270000"}
            await engine._on_fill(msg)

            # fills 기록 확인
            cur = await conn.execute("SELECT qty, price FROM fills WHERE intent_id = ?", (iid,))
            fill_row = await cur.fetchone()
            assert fill_row["qty"] == 4.0 and fill_row["price"] == 270000.0

            # intent 상태 FILLED 확인
            cur = await conn.execute("SELECT status FROM order_intents WHERE intent_id = ?", (iid,))
            intent_row = await cur.fetchone()
            assert intent_row["status"] == "FILLED"

            # 실잔고 더블 체크가 호출되었는지 검증
            mock_reconcile.assert_awaited_once_with(engine.client, conn)

    asyncio.run(run())

