# tests/test_backtest.py
import asyncio
from datetime import datetime, timedelta
import pandas as pd
import pytest

from core import db
from core.backtest.engine import BacktestEngine, Position
from core.backtest.metrics import calculate_backtest_metrics
from core.backtest.runner import BacktestConfig, BacktestRunner


def _create_synthetic_ohlcv(
    dates: list[str], start_price: float, trend: float = 0.0, drop_on_day: int = -1
) -> pd.DataFrame:
    rows = []
    price = start_price
    for i, d in enumerate(dates):
        if i == drop_on_day:
            open_p = price * 0.95
            low_p = price * 0.88
            high_p = price * 0.96
            close_p = price * 0.89
            price = close_p
        else:
            price = price * (1.0 + trend)
            open_p = price * 0.99
            high_p = price * 1.01
            low_p = price * 0.98
            close_p = price

        rows.append({
            "date": pd.to_datetime(d),
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": 10000.0,
        })
    return pd.DataFrame(rows)


def test_stop_loss_triggered():
    """평단 대비 -7% 이하로 급락 시 STOP_LOSS 청산 검증."""
    dates = [(datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(25)]
    df = _create_synthetic_ohlcv(dates, start_price=100000.0, drop_on_day=10)
    data = {"005930": df}

    engine = BacktestEngine(
        symbols=["005930"],
        data_by_symbol=data,
        initial_cash=10_000_000.0,
        stop_loss_pct=0.07,
    )
    engine.positions["005930"] = Position(
        symbol="005930",
        qty=50.0,
        avg_price=100000.0,
        peak_price=100000.0,
        entry_date="2025-01-01",
        entry_avg_price=100000.0,
    )
    engine.cash = 5_000_000.0

    result = engine.run("2025-01-01", dates[-1])
    stop_trades = [t for t in result.trades if t.side == "sell" and t.reason == "STOP_LOSS"]
    assert len(stop_trades) >= 1
    assert stop_trades[0].symbol == "005930"
    assert stop_trades[0].realized_pnl < 0


def test_trailing_take_profit_triggered():
    """상승 후 고점 대비 ATR 트레일링 폭만큼 하락 시 TRAILING_TAKE_PROFIT 청산 검증."""
    dates = [(datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(50)]
    rows = []
    p = 100000.0
    for i, d in enumerate(dates):
        if i <= 35:
            p *= 1.01
            rows.append({
                "date": pd.to_datetime(d),
                "open": p * 0.99,
                "high": p * 1.01,
                "low": p * 0.99,
                "close": p,
                "volume": 10000.0,
            })
        elif i == 36:
            p *= 0.94
            rows.append({
                "date": pd.to_datetime(d),
                "open": p * 1.01,
                "high": p * 1.01,
                "low": p * 0.99,
                "close": p,
                "volume": 10000.0,
            })
        else:
            rows.append({
                "date": pd.to_datetime(d),
                "open": p,
                "high": p * 1.01,
                "low": p * 0.99,
                "close": p,
                "volume": 10000.0,
            })

    df = pd.DataFrame(rows)
    data = {"000660": df}

    engine = BacktestEngine(
        symbols=["000660"],
        data_by_symbol=data,
        initial_cash=10_000_000.0,
    )
    engine.positions["000660"] = Position(
        symbol="000660",
        qty=30.0,
        avg_price=100000.0,
        peak_price=100000.0,
        entry_date="2025-01-01",
        entry_avg_price=100000.0,
    )
    engine.cash = 7_000_000.0

    result = engine.run("2025-01-01", dates[-1])
    trailing_trades = [t for t in result.trades if t.side == "sell" and t.reason == "TRAILING_TAKE_PROFIT"]
    assert len(trailing_trades) >= 1
    assert trailing_trades[0].realized_pnl > 0


def test_metrics_calculation():
    """지표(수익률, MDD, 샤프, 승률, 손익비) 계산 로직 검증."""
    dates = [(datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(55)]
    df_sym = _create_synthetic_ohlcv(dates, 10000.0, trend=0.005)
    df_bench = _create_synthetic_ohlcv(dates, 50000.0, trend=0.002)

    engine = BacktestEngine(
        symbols=["005930"],
        data_by_symbol={"005930": df_sym},
        initial_cash=10_000_000.0,
    )
    result = engine.run("2025-01-01", dates[-1])
    metrics = calculate_backtest_metrics(result, benchmark_df=df_bench)

    assert "strategy" in metrics
    assert "benchmark" in metrics
    assert "relative" in metrics
    assert "trade_stats" in metrics

    strat = metrics["strategy"]
    bench = metrics["benchmark"]
    assert strat["final_equity"] > 0
    assert "total_return_pct" in strat
    assert "sharpe" in strat
    assert "benchmark_return_pct" in bench


def test_backtest_runner_integration(tmp_path, monkeypatch):
    """BacktestRunner의 전체 파이프라인 통합 테스트."""
    async def run():
        test_db_path = str(tmp_path / "test_jarvis.db")
        monkeypatch.setattr("core.config.settings.DB_PATH", test_db_path)

        await db.init_db()
        conn = await db.get_connection()

        try:
            dates = [(datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(50)]
            bars_sym = [
                {"date": d, "open": 10000.0, "high": 10500.0, "low": 9800.0, "close": 10200.0, "volume": 5000.0}
                for d in dates
            ]
            bars_bench = [
                {"date": d, "open": 50000.0, "high": 50200.0, "low": 49800.0, "close": 50100.0, "volume": 10000.0}
                for d in dates
            ]

            await db.save_backtest_bars(conn, "005930", "domestic", bars_sym)
            await db.save_backtest_bars(conn, "360750", "domestic", bars_bench)

            runner = BacktestRunner(conn, client=None)
            cfg = BacktestConfig(
                symbols=["005930"],
                start_date="2025-01-01",
                end_date=dates[-1],
                benchmark_symbol="360750",
                initial_cash=10_000_000.0,
            )

            output = await runner.run(cfg)
            assert "metrics" in output
            assert "chart_data" in output
            assert len(output["chart_data"]) > 0
            assert output["chart_data"][0]["strategy_equity"] == 10_000_000.0
        finally:
            await conn.close()

    asyncio.run(run())


def test_trend_aware_signal_computation():
    """상승 추세(Uptrend) 및 하락 추세(Downtrend)에서 지표 방향성 검증."""
    from core.indicators import compute_technical_signal

    dates = [(datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(70)]
    
    # 강한 상승 추세: 매일 +1% 상승
    df_up = _create_synthetic_ohlcv(dates, 50000.0, trend=0.01)
    sig_up = compute_technical_signal(df_up)
    assert sig_up["direction"] == "BUY"
    assert sig_up["strength"] > 0.5

    # 강한 하락 추세: 매일 -1% 하락
    df_down = _create_synthetic_ohlcv(dates, 100000.0, trend=-0.01)
    sig_down = compute_technical_signal(df_down)
    assert sig_down["direction"] == "SELL"


def test_partial_trailing_take_profit_uptrend():
    """상승 추세 종목에서 트레일링 익절 발동 시 50% 분할 청산 검증."""
    dates = [(datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(90)]
    rows = []
    p = 100000.0
    for i, d in enumerate(dates):
        if i <= 65:
            p *= 1.015  # 지속적 상승 추세
            rows.append({
                "date": pd.to_datetime(d),
                "open": p * 0.99,
                "high": p * 1.01,
                "low": p * 0.99,
                "close": p,
                "volume": 10000.0,
            })
        elif i == 66:
            p *= 0.82  # 고점 대비 -18% 급락 (상승 추세 트레일링 트리거, len >= 60 성립)
            rows.append({
                "date": pd.to_datetime(d),
                "open": p * 1.01,
                "high": p * 1.01,
                "low": p * 0.98,
                "close": p,
                "volume": 10000.0,
            })
        else:
            rows.append({
                "date": pd.to_datetime(d),
                "open": p,
                "high": p * 1.01,
                "low": p * 0.99,
                "close": p,
                "volume": 10000.0,
            })

    df = pd.DataFrame(rows)
    data = {"005930": df}

    engine = BacktestEngine(
        symbols=["005930"],
        data_by_symbol=data,
        initial_cash=10_000_000.0,
    )
    # 초기 100주 보유 상태로 시작
    engine.positions["005930"] = Position(
        symbol="005930",
        qty=100.0,
        avg_price=100000.0,
        peak_price=100000.0,
        entry_date="2025-01-01",
        entry_avg_price=100000.0,
    )

    result = engine.run("2025-01-01", dates[-1])
    tp_trades = [t for t in result.trades if t.side == "sell" and t.reason == "TRAILING_TAKE_PROFIT"]
    assert len(tp_trades) >= 1
    # 50% 분할 매도(50주) 확인
    assert tp_trades[0].qty == 50.0
