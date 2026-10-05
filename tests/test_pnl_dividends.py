# tests/test_pnl_dividends.py
import pytest
from core import pnl

def test_daily_mtm_with_dividends():
    trades = [
        {"symbol": "005930", "market": "domestic", "date": "2026-04-01", "side": "buy", "qty": 10.0, "price": 70000.0, "fx": 1.0},
    ]
    opening_qty = {"005930": 0.0}
    closes = {
        "005930": {
            "2026-04-01": 70000.0,
            "2026-04-02": 71000.0,
            "2026-04-03": 71000.0,
        }
    }
    markets = {"005930": "domestic"}
    axis = ["2026-04-01", "2026-04-02", "2026-04-03"]
    fx = lambda ts: 1.0

    # 4월 2일에 배당금 5,000원 수령
    dividends_by_symbol_date = {
        "005930": {
            "2026-04-02": 5000.0,
        }
    }

    mtm = pnl.daily_mtm(
        trades=trades,
        opening_qty=opening_qty,
        closes=closes,
        markets=markets,
        axis=axis,
        fx=fx,
        dividends_by_symbol_date=dividends_by_symbol_date,
    )

    d1 = mtm["005930"]["2026-04-01"]
    assert d1["qty"] == 10.0
    assert d1["dividend_krw"] == 0.0

    d2 = mtm["005930"]["2026-04-02"]
    # 전일 종가 70,000 -> 당일 71,000 (시세차익 +10,000원) + 배당금 5,000원 = 15,000원
    assert d2["dividend_krw"] == 5000.0
    assert d2["pnl_krw"] == 15000.0

    d3 = mtm["005930"]["2026-04-03"]
    assert d3["dividend_krw"] == 0.0
    assert d3["pnl_krw"] == 0.0


def test_summarize_symbols_total_return():
    events = []
    trades = [
        {"symbol": "005930", "market": "domestic", "date": "2026-04-01", "side": "buy", "qty": 10.0, "price": 70000.0, "fx": 1.0, "estimated": False},
    ]
    state = {
        "005930": {"qty": 10.0, "avg_local": 70000.0, "avg_krw": 70000.0}
    }
    opening = {"005930": {"qty": 0.0, "avg_krw": 0.0}}
    holdings_end = {"005930": 10.0}
    last_close = {"005930": 75000.0}
    markets = {"005930": "domestic"}
    mtm = {}
    mismatch = {}
    fx_now = 1.0

    symbol_dividends = {"005930": 10000.0}

    summary = pnl.summarize_symbols(
        events=events,
        trades=trades,
        state=state,
        opening=opening,
        holdings_end=holdings_end,
        last_close=last_close,
        markets=markets,
        mtm=mtm,
        mismatch=mismatch,
        fx_now=fx_now,
        symbol_dividends=symbol_dividends,
    )

    assert len(summary) == 1
    row = summary[0]
    assert row["symbol"] == "005930"
    # 투자원금: 700,000원
    assert row["invested_krw"] == 700000.0
    # 평가손익: (75,000 - 70,000) * 10 = 50,000원
    assert row["unrealized_krw"] == 50000.0
    assert row["capital_pnl_krw"] == 50000.0
    # 배당금: 10,000원
    assert row["dividend_krw"] == 10000.0
    # 총손익: 60,000원
    assert row["total_pnl_krw"] == 60000.0
    # Price ROI: 50,000 / 700,000 = ~0.0714
    assert pytest.approx(row["price_roi"], rel=1e-3) == 50000.0 / 700000.0
    # Total ROI: 60,000 / 700,000 = ~0.0857
    assert pytest.approx(row["roi"], rel=1e-3) == 60000.0 / 700000.0
