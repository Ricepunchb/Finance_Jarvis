# tests/test_market_regime.py
"""시장 전체 레짐(RISK_ON / NEUTRAL / RISK_OFF) 판정 및 방어 자산 예외 테스트."""
import pandas as pd

from core.market_regime import MarketRegimeCache, evaluate_market_regime, is_defensive_asset


def _bench_df(closes):
    return pd.DataFrame({
        "date": pd.date_range("2026-08-01", periods=len(closes), freq="D"),
        "close": closes,
    })


def test_regime_triggers_risk_off_on_black_monday_drop():
    df = _bench_df([100.0] * 25)
    res = evaluate_market_regime(df, benchmark_symbol="069500", intraday_change_pct=-0.035)
    assert res.regime == "RISK_OFF"
    assert "급락" in res.reason


def test_regime_triggers_risk_off_below_sma20_with_program_sell():
    df = _bench_df([100.0] * 20 + [97.0])
    res = evaluate_market_regime(
        df,
        benchmark_symbol="069500",
        intraday_change_pct=-0.005,
        market_program_net=-150000.0,
    )
    assert res.regime == "RISK_OFF"
    assert res.below_sma20 is True


def test_regime_risk_on_in_uptrend():
    df = _bench_df([100.0 + i * 0.5 for i in range(65)])
    res = evaluate_market_regime(df, benchmark_symbol="069500", intraday_change_pct=0.008)
    assert res.regime == "RISK_ON"


def test_defensive_asset_detection():
    assert is_defensive_asset("365780", "ACE 국고채10년") is True
    assert is_defensive_asset("411060", "ACE KRX금현물") is True
    assert is_defensive_asset("SGOV", "") is True
    assert is_defensive_asset("005930", "삼성전자") is False


def test_regime_cache_ttl():
    cache = MarketRegimeCache(ttl_sec=60)
    r = evaluate_market_regime(None, benchmark_symbol="069500", intraday_change_pct=0.0)
    cache.set("domestic", r, now=1000.0)
    assert cache.get("domestic", now=1030.0) == r
    assert cache.get("domestic", now=1061.0) is None
