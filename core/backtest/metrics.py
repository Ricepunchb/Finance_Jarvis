# core/backtest/metrics.py
"""백테스팅 성과 및 벤치마크(S&P 500 ETF) 비교 지표 계산 모듈."""
import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from core.backtest.engine import BacktestResult, TradeRecord
from core.config import settings

TRADING_DAYS_PER_YEAR = 252


def calculate_backtest_metrics(
    result: BacktestResult,
    benchmark_df: Optional[pd.DataFrame] = None,
    risk_free_rate_annual: float = settings.RISK_FREE_RATE_ANNUAL,
) -> Dict[str, Any]:
    """백테스트 결과와 벤치마크(S&P 500 ETF)를 대조 분석하여 종합 메트릭 딕셔너리를 생성."""
    if not result.daily_snapshots:
        return {}

    # 1. 일별 자산 시계열 DataFrame 구성
    dates = [s.date for s in result.daily_snapshots]
    equities = [s.total_equity for s in result.daily_snapshots]
    cash_vals = [s.cash for s in result.daily_snapshots]
    daily_rets = [s.daily_return for s in result.daily_snapshots]

    df_strat = pd.DataFrame({
        "date": pd.to_datetime(dates),
        "equity": equities,
        "cash": cash_vals,
        "daily_return": daily_rets,
    }).sort_values("date").reset_index(drop=True)

    # 2. 전략 기본 지표 계산
    total_ret_pct = result.total_return_pct
    n_days = len(df_strat)
    years = max(n_days / TRADING_DAYS_PER_YEAR, 0.01)

    cagr_pct = float(((df_strat["equity"].iloc[-1] / df_strat["equity"].iloc[0]) ** (1.0 / years) - 1.0) * 100.0) if df_strat["equity"].iloc[0] > 0 else 0.0

    running_max = df_strat["equity"].cummax()
    drawdown = (df_strat["equity"] - running_max) / running_max
    mdd_pct = float(drawdown.min() * 100.0)

    strat_rets = df_strat["daily_return"].iloc[1:]  # 첫날 수익률(0) 제외
    daily_rf = risk_free_rate_annual / TRADING_DAYS_PER_YEAR
    ann_factor = math.sqrt(TRADING_DAYS_PER_YEAR)

    vol_pct = float(strat_rets.std() * ann_factor * 100.0) if len(strat_rets) > 1 else 0.0

    excess_ret = strat_rets - daily_rf
    sharpe = float((excess_ret.mean() / strat_rets.std()) * ann_factor) if strat_rets.std() > 0 else 0.0

    downside = strat_rets[strat_rets < 0]
    downside_std = downside.std() if len(downside) > 1 else 0.0
    sortino = float((excess_ret.mean() / downside_std) * ann_factor) if downside_std > 0 else 0.0

    # 3. 벤치마크(S&P 500 ETF) 비교 지표 계산
    bench_metrics: Dict[str, Any] = {}
    alpha = None
    beta = None
    corr = None

    if benchmark_df is not None and not benchmark_df.empty:
        df_bench = benchmark_df[["date", "close"]].copy()
        df_bench["date"] = pd.to_datetime(df_bench["date"])
        df_bench = df_bench.sort_values("date").reset_index(drop=True)

        # 전략 날짜와 inner join
        merged = pd.merge(df_strat[["date", "equity", "daily_return"]], df_bench, on="date", how="inner")
        if len(merged) >= 5:
            b_close = merged["close"]
            b_ret_pct = float((b_close.iloc[-1] / b_close.iloc[0] - 1.0) * 100.0)
            b_years = max(len(merged) / TRADING_DAYS_PER_YEAR, 0.01)
            b_cagr_pct = float(((b_close.iloc[-1] / b_close.iloc[0]) ** (1.0 / b_years) - 1.0) * 100.0)

            b_max = b_close.cummax()
            b_mdd_pct = float(((b_close - b_max) / b_max).min() * 100.0)

            b_daily_rets = b_close.pct_change().dropna()
            b_vol_pct = float(b_daily_rets.std() * ann_factor * 100.0) if len(b_daily_rets) > 1 else 0.0

            b_excess = b_daily_rets - daily_rf
            b_sharpe = float((b_excess.mean() / b_daily_rets.std()) * ann_factor) if b_daily_rets.std() > 0 else 0.0

            bench_metrics = {
                "benchmark_return_pct": round(b_ret_pct, 2),
                "benchmark_cagr_pct": round(b_cagr_pct, 2),
                "benchmark_mdd_pct": round(b_mdd_pct, 2),
                "benchmark_volatility_pct": round(b_vol_pct, 2),
                "benchmark_sharpe": round(b_sharpe, 2),
            }

            # Beta & Alpha & Correlation
            s_aligned = merged["daily_return"].iloc[1:]
            b_aligned = b_daily_rets
            if len(s_aligned) >= 5 and b_aligned.var() > 0:
                cov = s_aligned.cov(b_aligned)
                b_var = b_aligned.var()
                beta = float(cov / b_var)
                corr = float(s_aligned.corr(b_aligned))
                # 연율화 알파 (Jensen's Alpha)
                s_ann_ret = s_aligned.mean() * TRADING_DAYS_PER_YEAR
                b_ann_ret = b_aligned.mean() * TRADING_DAYS_PER_YEAR
                alpha = float((s_ann_ret - risk_free_rate_annual) - beta * (b_ann_ret - risk_free_rate_annual))

    # 4. 매매 내역 통계 (Win Rate, Profit Factor, 손절 횟수 등)
    trade_stats = _compute_trade_statistics(result.trades)

    return {
        "period": {
            "start_date": result.start_date,
            "end_date": result.end_date,
            "trading_days": n_days,
            "years": round(years, 2),
        },
        "strategy": {
            "initial_cash": result.initial_cash,
            "final_equity": round(result.final_equity, 0),
            "total_return_pct": round(total_ret_pct, 2),
            "cagr_pct": round(cagr_pct, 2),
            "mdd_pct": round(mdd_pct, 2),
            "volatility_pct": round(vol_pct, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
        },
        "benchmark": bench_metrics,
        "relative": {
            "excess_return_pct": round(total_ret_pct - (bench_metrics.get("benchmark_return_pct") or 0.0), 2),
            "beta": round(beta, 2) if beta is not None else None,
            "alpha_pct": round(alpha * 100.0, 2) if alpha is not None else None,
            "correlation": round(corr, 2) if corr is not None else None,
        },
        "trade_stats": trade_stats,
    }


def _compute_trade_statistics(trades: List[TradeRecord]) -> Dict[str, Any]:
    """청산 매매 기준 승률, 손익비, 사유별 통계 산출."""
    sells = [t for t in trades if t.side == "sell"]
    if not sells:
        return {
            "total_trades": len(trades),
            "buy_trades": len(trades) - len(sells),
            "sell_trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "stop_loss_count": 0,
            "trailing_stop_count": 0,
            "signal_sell_count": 0,
            "total_realized_pnl": 0.0,
            "total_fees_and_taxes": sum(t.fee + t.tax for t in trades),
        }

    wins = [t for t in sells if t.realized_pnl > 0]
    losses = [t for t in sells if t.realized_pnl <= 0]

    win_rate = (len(wins) / len(sells)) * 100.0 if sells else 0.0

    total_gain = sum(t.realized_pnl for t in wins)
    total_loss = abs(sum(t.realized_pnl for t in losses))
    profit_factor = (total_gain / total_loss) if total_loss > 0 else (99.0 if total_gain > 0 else 0.0)

    stop_losses = [t for t in sells if "STOP_LOSS" in t.reason]
    trailing_stops = [t for t in sells if "TRAILING_TAKE_PROFIT" in t.reason]
    signal_sells = [t for t in sells if "swing_signal" in t.reason or "forced_trim" in t.reason]

    return {
        "total_trades": len(trades),
        "buy_trades": len(trades) - len(sells),
        "sell_trades": len(sells),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "stop_loss_count": len(stop_losses),
        "trailing_stop_count": len(trailing_stops),
        "signal_sell_count": len(signal_sells),
        "total_realized_pnl": round(sum(t.realized_pnl for t in sells), 0),
        "total_fees_and_taxes": round(sum(t.fee + t.tax for t in trades), 0),
    }
