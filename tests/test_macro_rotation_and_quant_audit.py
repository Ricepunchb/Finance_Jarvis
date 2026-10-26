import asyncio
import time
import pandas as pd

from core import db, discovery, exit_guard, indicators, signal_engine
from core.config import settings
from core.llm.gemini_provider import GeminiProvider
from core.risk import RiskManager


def test_gemini_prompt_contains_macro_rotation_directive():
    captured_prompt = {}

    class DummyProvider(GeminiProvider):
        def __init__(self):
            pass

        async def _generate(self, prompt: str, schema, temperature: float = 0.3):
            captured_prompt["text"] = prompt
            return schema(
                adds=[{"symbol": "012450", "name": "한화에어로스페이스", "rationale": "지정학 리스크 방산 헤지"}],
                removes=[],
                weights=[
                    {"symbol": "005930", "weight": 0.65},
                    {"symbol": "012450", "weight": 0.35},
                ],
                rationale="매크로 로테이션 반영",
            )

    provider = DummyProvider()
    res = asyncio.run(
        provider.propose_portfolio_changes(
            current_positions=[
                {"symbol": "005930", "name": "삼성전자", "weight": 0.25, "thesis": "반도체 코어"}
            ],
            current_signals={},
            candidate_pool=[
                {
                    "symbol": "012450",
                    "name": "한화에어로스페이스",
                    "universe_tag": "KR_DEFENSE",
                    "composite_score": 0.48,
                    "tech": {"direction": "BUY", "strength": 0.72, "detail": "score=0.72"},
                    "sentiment": {"direction": "BUY", "strength": 0.6, "reasoning": "수주 모멘텀"},
                    "valuation": {"per": 18.0, "pbr": 2.5},
                },
            ],
            macro_context="Fear & Greed: 18 (Extreme Fear)",
            insight_digest=None,
            max_symbols=10,
            min_weight=0.05,
            max_weight=0.30,
        )
    )
    prompt = captured_prompt["text"]
    assert "글로벌 매크로 로테이션(Macro Rotation)" in prompt
    assert "수동적 현금 도피(Cash Hiding)를 지양하라" in prompt
    assert "방산, 정유/에너지" in prompt
    assert "012450" in res["weights"]


def test_compute_technical_signal_includes_trend_flags():
    # 상승 추세(SMA20 >= SMA60, Close >= SMA20) 데이터 생성
    prices_up = [100.0 + i * 0.8 for i in range(70)]
    df_up = pd.DataFrame({
        "open": prices_up,
        "high": [p * 1.01 for p in prices_up],
        "low": [p * 0.99 for p in prices_up],
        "close": prices_up,
        "volume": [1000] * 70,
    })
    sig_up = indicators.compute_technical_signal(df_up)
    assert sig_up["is_uptrend"] is True
    assert sig_up["is_downtrend"] is False
    assert sig_up["above_sma20"] is True

    # 하락 추세(SMA20 < SMA60, Close < SMA20) 데이터 생성
    prices_down = [200.0 - i * 1.0 for i in range(70)]
    df_down = pd.DataFrame({
        "open": prices_down,
        "high": [p * 1.01 for p in prices_down],
        "low": [p * 0.99 for p in prices_down],
        "close": prices_down,
        "volume": [1000] * 70,
    })
    sig_down = indicators.compute_technical_signal(df_down)
    assert sig_down["is_uptrend"] is False
    assert sig_down["is_downtrend"] is True
    assert sig_down["above_sma20"] is False


def test_signal_engine_blocks_falling_knife_but_allows_uptrend_and_breakout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "test_sig.db"))
    monkeypatch.setattr(settings, "ONBOARDING_ENABLED", True)
    monkeypatch.setattr(settings, "REQUIRE_UPTREND_FOR_ONBOARDING", True)
    monkeypatch.setattr(settings, "REQUIRE_SMA20_FOR_BUY", True)

    async def run():
        await db.init_db()
        conn = await db.get_connection()
        try:
            risk = RiskManager(conn)
            # 1) 확정적 하락추세(is_downtrend=True) 종목은 온보딩(저비중) 매수 차단
            act_falling = await signal_engine.decide(
                symbol="005930",
                current_weight=0.0,
                target_weight=0.20,
                band=0.05,
                tech_signal={"direction": "HOLD", "strength": 0.2, "is_uptrend": False, "is_downtrend": True, "above_sma20": False},
                intraday_signal={"direction": "BUY", "strength": 0.4},
                sentiment_signal=None,
                price=50000.0,
                current_position_qty=0.0,
                current_position_value=0.0,
                total_equity=10_000_000.0,
                risk=risk,
                accumulation_mode=False,
            )
            assert act_falling is None

            # 2) 바닥권에서 SMA20을 돌파한 초동 반등(is_downtrend=False, above_sma20=True) 종목은 온보딩 진입 허용
            act_breakout = await signal_engine.decide(
                symbol="012450",
                current_weight=0.0,
                target_weight=0.20,
                band=0.05,
                tech_signal={"direction": "BUY", "strength": 0.4, "is_uptrend": False, "is_downtrend": False, "above_sma20": True},
                intraday_signal={"direction": "HOLD", "strength": 0.1},
                sentiment_signal=None,
                price=100000.0,
                current_position_qty=0.0,
                current_position_value=0.0,
                total_equity=10_000_000.0,
                risk=risk,
                accumulation_mode=False,
            )
            assert act_breakout is not None
            assert act_breakout.side == "buy"
            assert act_breakout.reason == "onboarding_tranche"

            # 3) 상승추세 내 눌림목(is_uptrend=True, is_downtrend=False) 종목도 정상 진입 허용
            act_pullback = await signal_engine.decide(
                symbol="105560",
                current_weight=0.0,
                target_weight=0.20,
                band=0.05,
                tech_signal={"direction": "BUY", "strength": 0.8, "is_uptrend": True, "is_downtrend": False, "above_sma20": False},
                intraday_signal={"direction": "BUY", "strength": 0.6},
                sentiment_signal=None,
                price=80000.0,
                current_position_qty=0.0,
                current_position_value=0.0,
                total_equity=10_000_000.0,
                risk=risk,
                accumulation_mode=False,
            )
            assert act_pullback is not None
            assert act_pullback.side == "buy"
        finally:
            await conn.close()

    asyncio.run(run())


def test_stop_loss_cooldown_days_enforced_in_risk_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "test_sl_cooldown.db"))
    monkeypatch.setattr(settings, "ORDER_COOLDOWN_SEC", 1800)
    monkeypatch.setattr(settings, "STOP_LOSS_COOLDOWN_DAYS", 5)

    async def run():
        await db.init_db()
        conn = await db.get_connection()
        try:
            risk = RiskManager(conn)
            cycle_id = await db.new_cycle(conn)
            intent_id = await db.create_order_intent(
                conn, cycle_id, "005930", "sell", 10.0, "market", None, "STOP_LOSS"
            )
            await db.update_order_intent(conn, intent_id, status="FILLED")
            # 주문 시각을 2일 전(일반 30분 쿨다운은 지났지만 5일 손절 쿨다운 내)으로 조정
            two_days_ago = time.time() - (2 * 86400)
            await conn.execute(
                "UPDATE order_intents SET created_at = ? WHERE intent_id = ?",
                (two_days_ago, intent_id),
            )
            await conn.commit()

            assert await risk.is_cooldown_active("005930") is True

            # 6일 전으로 변경하면 쿨다운 해제
            six_days_ago = time.time() - (6 * 86400)
            await conn.execute(
                "UPDATE order_intents SET created_at = ? WHERE intent_id = ?",
                (six_days_ago, intent_id),
            )
            await conn.commit()
            assert await risk.is_cooldown_active("005930") is False
        finally:
            await conn.close()

    asyncio.run(run())


def test_exit_guard_uptrend_partial_take_profit():
    # 상승 추세(is_uptrend=True)에서는 트레일링 익절 시 50% 분할 매도(sell_ratio=0.5)
    decision_up = exit_guard.evaluate_exit(
        avg_price=100_000.0,
        peak_price=112_000.0,  # +12% 고점 (>= 8% 무장 기준)
        current_price=103_000.0,  # 고점 대비 -8.04% 하락 (<= -7.5% 트레일링 폭)
        atr_pct=0.02,
        is_uptrend=True,
    )
    assert decision_up is not None
    assert decision_up.reason == "TRAILING_TAKE_PROFIT"
    assert decision_up.sell_ratio == 0.5

    # 비추세(is_uptrend=False)에서는 전량 매도(sell_ratio=1.0)
    decision_flat = exit_guard.evaluate_exit(
        avg_price=100_000.0,
        peak_price=110_000.0,
        current_price=104_000.0,
        atr_pct=0.02,
        is_uptrend=False,
    )
    assert decision_flat is not None
    assert decision_flat.reason == "TRAILING_TAKE_PROFIT"
    assert decision_flat.sell_ratio == 1.0


def test_macro_rotation_tags_in_discovery():
    assert "KR_DEFENSE" in discovery.MACRO_ROTATION_TAGS
    assert "KR_ENERGY_COMMODITY" in discovery.MACRO_ROTATION_TAGS
    assert "KR_SAFE_HAVEN" in discovery.MACRO_ROTATION_TAGS
    assert "KR_DIVIDEND_FINANCE" in discovery.MACRO_ROTATION_TAGS
    assert "KR_GLOBAL_ETF" in discovery.MACRO_ROTATION_TAGS
    assert settings.DISCOVERY_MIN_SCORE <= 0.30
