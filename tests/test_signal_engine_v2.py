# tests/test_signal_engine_v2.py
"""개정 signal_engine 검증: 뉴스 방향성 제외, 악재 Veto, 수급 확인/역행, RISK_OFF 차단."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from core import signal_engine


def _mock_risk():
    risk = MagicMock()
    risk.onboarding_notional_today = AsyncMock(return_value=0.0)
    risk.clamp_qty_to_notional = MagicMock(side_effect=lambda q, p, **kw: q)
    risk.clamp_buy_qty_to_position_cap = MagicMock(side_effect=lambda q, p, v, eq: q)
    return risk


def test_combine_signals_ignores_sentiment_for_direction():
    tech = {"direction": "BUY", "strength": 0.6}
    intraday = {"direction": "BUY", "strength": 0.6}
    # 뉴스가 HOLD(0.0)여도 기술적 시그널이 희석되지 않아야 한다
    with_hold_news = signal_engine.combine_signals(
        tech, intraday, sentiment={"direction": "HOLD", "strength": 0.0}
    )
    without_news = signal_engine.combine_signals(tech, intraday, sentiment=None)
    assert with_hold_news["direction"] == "BUY"
    assert with_hold_news["strength"] == without_news["strength"]


def test_combine_signals_flow_confirmation_boost_and_divergence_veto():
    tech = {"direction": "BUY", "strength": 0.6}
    intraday = {"direction": "BUY", "strength": 0.6}
    flow_buy = {"direction": "BUY", "strength": 0.7}
    confirmed = signal_engine.combine_signals(tech, intraday, flow=flow_buy, flow_confirm_boost=1.15)
    assert confirmed["direction"] == "BUY"
    assert "flow_confirmed_buy" in confirmed.get("detail", "")
    assert confirmed["strength"] > 0.65

    # 기술 지표는 BUY인데 고래 수급이 강한 SELL(0.75)이면 매수 억제(HOLD)
    flow_sell = {"direction": "SELL", "strength": 0.75}
    vetoed = signal_engine.combine_signals(tech, intraday, flow=flow_sell)
    assert vetoed["direction"] == "HOLD"
    assert "flow_divergence_veto" in vetoed.get("detail", "")


def test_bad_news_veto_blocks_buy_but_allows_sell():
    risk = _mock_risk()
    bad_news = {"direction": "SELL", "strength": 0.85}

    async def run():
        buy_action = await signal_engine.decide(
            symbol="005930",
            current_weight=0.05,
            target_weight=0.15,
            band=0.05,
            tech_signal={"direction": "BUY", "strength": 0.8, "is_downtrend": False},
            intraday_signal={"direction": "BUY", "strength": 0.8},
            sentiment_signal=bad_news,
            flow_signal={"direction": "BUY", "strength": 0.8},
            price=70000.0,
            current_position_qty=1.0,
            current_position_value=70000.0,
            total_equity=2_000_000.0,
            risk=risk,
            accumulation_mode=False,
        )
        sell_action = await signal_engine.decide(
            symbol="005930",
            current_weight=0.15,
            target_weight=0.15,
            band=0.05,
            tech_signal={"direction": "SELL", "strength": 0.8, "is_downtrend": True},
            intraday_signal={"direction": "SELL", "strength": 0.8},
            sentiment_signal=bad_news,
            flow_signal={"direction": "SELL", "strength": 0.8},
            price=70000.0,
            current_position_qty=5.0,
            current_position_value=350000.0,
            total_equity=2_000_000.0,
            risk=risk,
            accumulation_mode=False,
        )
        return buy_action, sell_action

    buy_action, sell_action = asyncio.run(run())
    assert buy_action is None
    assert sell_action is not None
    assert sell_action.side == "sell"


def test_risk_off_blocks_equity_buy_but_allows_defensive_etf():
    risk = _mock_risk()

    async def run():
        equity_buy = await signal_engine.decide(
            symbol="005930",
            current_weight=0.0,
            target_weight=0.15,
            band=0.05,
            tech_signal={"direction": "BUY", "strength": 0.8, "is_downtrend": False},
            intraday_signal={"direction": "BUY", "strength": 0.8},
            sentiment_signal=None,
            flow_signal={"direction": "BUY", "strength": 0.8},
            market_regime="RISK_OFF",
            is_defensive=False,
            price=70000.0,
            current_position_qty=0.0,
            current_position_value=0.0,
            total_equity=2_000_000.0,
            risk=risk,
            accumulation_mode=False,
        )
        bond_buy = await signal_engine.decide(
            symbol="365780",
            current_weight=0.0,
            target_weight=0.15,
            band=0.05,
            tech_signal={"direction": "BUY", "strength": 0.8, "is_downtrend": False},
            intraday_signal={"direction": "BUY", "strength": 0.8},
            sentiment_signal=None,
            flow_signal={"direction": "BUY", "strength": 0.8},
            market_regime="RISK_OFF",
            is_defensive=True,
            price=86000.0,
            current_position_qty=0.0,
            current_position_value=0.0,
            total_equity=2_000_000.0,
            risk=risk,
            accumulation_mode=False,
        )
        return equity_buy, bond_buy

    equity_buy, bond_buy = asyncio.run(run())
    assert equity_buy is None
    assert bond_buy is not None
    assert bond_buy.side == "buy"
