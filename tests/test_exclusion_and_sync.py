import asyncio

import aiosqlite

from core import db, signal_engine
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
    assert _decide_onboard(0.0, prior_notional=5_700_000) is None  # 일일 예산(0.08*352M/5=5.6M) 소진


def test_onboarding_allows_one_share_when_price_exceeds_order_cap():
    a = _decide_onboard(0.0, price=1_800_000)
    assert a.qty == 1
