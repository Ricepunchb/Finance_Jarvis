# tests/test_flow_signals.py
"""고래/수급 시그널(Layer A OHLCV 프록시 + Layer B KIS 수급) 단위 테스트."""
import asyncio
from unittest.mock import AsyncMock, patch

import pandas as pd

from core import flow_signals
from core.kis_common import KisApiError


def _make_df(closes, volumes, opens=None, highs=None, lows=None):
    n = len(closes)
    opens = opens or closes
    highs = highs or [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = lows or [min(o, c) * 0.99 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "date": pd.date_range("2026-10-01", periods=n, freq="D"),
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_ohlcv_flow_detects_whale_accumulation():
    # 25봉 완만 상승 + 마지막 봉 거래량 3배 급증 + 양봉(종가가 고가 근처)
    closes = [100.0 + i * 0.5 for i in range(24)] + [116.0]
    opens = [100.0 + i * 0.5 - 0.3 for i in range(24)] + [112.0]
    highs = [c + 0.2 for c in closes]
    lows = [o - 0.5 for o in opens]
    volumes = [1000.0] * 24 + [3500.0]

    df = _make_df(closes, volumes, opens=opens, highs=highs, lows=lows)
    sig = flow_signals.compute_ohlcv_flow_signal(df)
    assert sig["direction"] == "BUY"
    assert sig["strength"] > 0.5
    assert sig["components"]["rvol"] > 0.5
    assert sig["components"]["cmf"] > 0.0


def test_ohlcv_flow_detects_whale_distribution():
    closes = [120.0 - i * 0.5 for i in range(24)] + [100.0]
    opens = [120.0 - i * 0.5 + 0.3 for i in range(24)] + [108.0]
    highs = [o + 0.5 for o in opens]
    lows = [c - 0.2 for c in closes]
    volumes = [1000.0] * 24 + [4000.0]

    df = _make_df(closes, volumes, opens=opens, highs=highs, lows=lows)
    sig = flow_signals.compute_ohlcv_flow_signal(df)
    assert sig["direction"] == "SELL"
    assert sig["strength"] > 0.5
    assert sig["components"]["rvol"] < -0.5


def test_ohlcv_flow_insufficient_bars_returns_hold():
    df = _make_df([100.0] * 10, [1000.0] * 10)
    sig = flow_signals.compute_ohlcv_flow_signal(df)
    assert sig["direction"] == "HOLD"
    assert sig["strength"] == 0.0


def test_kis_flow_combines_program_investor_and_cttr():
    program_rows = [{"whol_smtn_ntby_qty": "50000", "whol_smtn_shnu_vol": "80000", "whol_smtn_seln_vol": "30000"}]
    investor_rows = [
        {"frgn_ntby_qty": "30000", "orgn_ntby_qty": "20000", "prsn_ntby_qty": "-50000"},
    ]
    sig = flow_signals.compute_kis_flow_signal(
        program_rows=program_rows,
        investor_rows=investor_rows,
        cttr=135.0,
    )
    assert sig is not None
    assert sig["direction"] == "BUY"
    assert sig["strength"] > 0.6


def test_domestic_flow_gracefully_falls_back_to_layer_a_on_mock_kis_error():
    closes = [100.0 + i * 0.4 for i in range(24)] + [115.0]
    opens = [c - 0.5 for c in closes]
    volumes = [1000.0] * 24 + [3000.0]
    df = _make_df(closes, volumes, opens=opens)

    async def run():
        with (
            patch("core.kis_domestic.get_program_trade_by_stock", new=AsyncMock(side_effect=KisApiError("EGW02006", "mock", {}))),
            patch("core.kis_domestic.get_investor_trend", new=AsyncMock(side_effect=KisApiError("EGW02006", "mock", {}))),
            patch("core.kis_domestic.get_recent_executions", new=AsyncMock(side_effect=KisApiError("EGW02006", "mock", {}))),
        ):
            return await flow_signals.get_domestic_flow_signal(object(), "005930", df)

    sig = asyncio.run(run())
    assert sig["direction"] == "BUY"
    assert sig["strength"] > 0.0
