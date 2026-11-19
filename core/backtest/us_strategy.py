# core/backtest/us_strategy.py
"""미국장 전용 국면 적응형 듀얼 소티노(Regime-Adaptive Dual-Sortino) 백테스트 시뮬레이터.

도메인 전략 코어(`core/us_market/strategy.py`)의 단일 벡터화 파이프라인(`compute_dual_sortino_matrices`)과
단일 설정 객체(`USDualSortinoConfig`)를 그대로 사용하여, 백테스트와 실전 운영 엔진 간의
수식 중복(DRY 위반) 및 구현 괴리(Implementation Drift)를 원천 차단합니다.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from core.backtest.engine import BacktestResult, DailySnapshot, Position, TradeRecord
from core.us_market.strategy import (
    CASH_PARKING_ETFS,
    DEFAULT_US_STRATEGY_CONFIG,
    DEFENSIVE_CORE_ETFS,
    TACTICAL_HIGH_BETA_ETFS,
    TRADING_DAYS_PER_YEAR,
    USDualSortinoConfig,
    classify_us_symbol_role,
    compute_dual_sortino_matrices,
    compute_us_live_optimal_weights,
    evaluate_us_live_symbol_signal,
    is_us_portfolio,
)

__all__ = [
    "CASH_PARKING_ETFS",
    "DEFAULT_US_STRATEGY_CONFIG",
    "DEFENSIVE_CORE_ETFS",
    "TACTICAL_HIGH_BETA_ETFS",
    "TRADING_DAYS_PER_YEAR",
    "USDualSortinoConfig",
    "classify_us_symbol_role",
    "compute_dual_sortino_matrices",
    "compute_us_live_optimal_weights",
    "evaluate_us_live_symbol_signal",
    "is_us_portfolio",
    "run_us_adaptive_backtest",
]


def run_us_adaptive_backtest(
    symbols: List[str],
    data_by_symbol: Dict[str, pd.DataFrame],
    start_date: str,
    end_date: str,
    initial_cash: float = 10_000_000.0,
    fee_rate: float = 0.00015,
    slippage_pct: float = 0.0005,
    config: USDualSortinoConfig = DEFAULT_US_STRATEGY_CONFIG,
) -> BacktestResult:
    """도메인 코어(`compute_dual_sortino_matrices`)를 사용하는 미국장 전용 백테스트 실행기."""
    mats = compute_dual_sortino_matrices(symbols, data_by_symbol, config=config)
    if mats is None:
        return BacktestResult(
            initial_cash=initial_cash,
            final_equity=initial_cash,
            total_return_pct=0.0,
            daily_snapshots=[],
            trades=[],
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )

    close_df = mats.close_df
    full_dt = close_df.index
    sim_dt = full_dt[(full_dt >= pd.to_datetime(start_date)) & (full_dt <= pd.to_datetime(end_date))]
    if len(sim_dt) == 0:
        return BacktestResult(
            initial_cash=initial_cash,
            final_equity=initial_cash,
            total_return_pct=0.0,
            daily_snapshots=[],
            trades=[],
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )

    cash = float(initial_cash)
    positions: Dict[str, Position] = {}
    trades: List[TradeRecord] = []
    snapshots: List[DailySnapshot] = []
    prev_equity = cash

    cur_target_a: Optional[str] = None
    cur_target_b: Optional[str] = None
    is_short_synthetic = len(full_dt) < 35

    for idx, dt in enumerate(sim_dt):
        cur_date_str = dt.strftime("%Y-%m-%d")

        if is_short_synthetic:
            row_fb = mats.fallback_score.loc[dt]
            target_sym = str(row_fb.idxmax()) if row_fb.max() > -900 else mats.active_syms[0]
        else:
            if idx % config.model_a_rebalance_days == 0:
                row_a = mats.score_a.loc[dt]
                cur_target_a = str(row_a.idxmax()) if row_a.max() > -900 else None

            if idx % config.model_b_rebalance_days == 0:
                row_b = mats.score_b.loc[dt]
                cur_target_b = str(row_b.idxmax()) if row_b.max() > -900 else None

            b_val = float(mats.breadth_ma15.loc[dt])
            target_sym = cur_target_b if b_val <= config.breadth_defensive_threshold else cur_target_a

        # 1. 목표 종목 변경 시 기존 포지션 전량 청산 (미국장 증권거래세 0.0)
        for held_sym in list(positions.keys()):
            if held_sym != target_sym:
                pos = positions[held_sym]
                c_p = float(close_df.loc[dt, held_sym])
                exec_p = c_p * (1.0 - slippage_pct)
                notional = pos.qty * exec_p
                fee = notional * fee_rate
                net_proceeds = notional - fee
                cost_basis = pos.avg_price * pos.qty
                realized_pnl = net_proceeds - cost_basis
                ret_pct = ((net_proceeds / cost_basis) - 1.0) * 100.0 if cost_basis > 0 else 0.0

                cash += net_proceeds
                del positions[held_sym]
                trades.append(
                    TradeRecord(
                        symbol=held_sym,
                        date=cur_date_str,
                        side="sell",
                        price=exec_p,
                        qty=pos.qty,
                        notional=notional,
                        fee=fee,
                        tax=0.0,
                        reason="TRAILING_TAKE_PROFIT" if realized_pnl > 0 else "us_regime_rotation_exit",
                        realized_pnl=realized_pnl,
                        return_pct=ret_pct,
                    )
                )

        # 2. 신규 목표 종목 진입
        if target_sym is not None and target_sym not in positions:
            c_p = float(close_df.loc[dt, target_sym])
            if c_p > 0 and cash > c_p * (1.0 + slippage_pct) * (1.0 + fee_rate):
                exec_p = c_p * (1.0 + slippage_pct)
                buy_qty = float(int((cash / (1.0 + fee_rate)) / exec_p))
                if buy_qty > 0:
                    notional = buy_qty * exec_p
                    fee = notional * fee_rate
                    total_cost = notional + fee
                    cash -= total_cost
                    positions[target_sym] = Position(
                        symbol=target_sym,
                        qty=buy_qty,
                        avg_price=total_cost / buy_qty,
                        peak_price=exec_p,
                        entry_date=cur_date_str,
                        entry_avg_price=exec_p,
                    )
                    trades.append(
                        TradeRecord(
                            symbol=target_sym,
                            date=cur_date_str,
                            side="buy",
                            price=exec_p,
                            qty=buy_qty,
                            notional=notional,
                            fee=fee,
                            tax=0.0,
                            reason="us_sortino_leader_entry",
                        )
                    )

        # 3. 당일 종가 스냅샷 기록
        pos_val = 0.0
        pos_dict: Dict[str, Dict[str, float]] = {}
        for sym, pos in positions.items():
            p = float(close_df.loc[dt, sym])
            pos.peak_price = max(pos.peak_price, p)
            val = pos.qty * p
            pos_val += val
            pos_dict[sym] = {"qty": pos.qty, "price": p, "value": val}

        total_eq = cash + pos_val
        daily_ret = ((total_eq / prev_equity) - 1.0) if idx > 0 and prev_equity > 0 else 0.0
        snapshots.append(
            DailySnapshot(
                date=cur_date_str,
                cash=cash,
                positions_value=pos_val,
                total_equity=total_eq,
                daily_return=daily_ret,
                positions=pos_dict,
            )
        )
        prev_equity = total_eq

    final_eq = snapshots[-1].total_equity if snapshots else initial_cash
    tot_ret = float((final_eq / initial_cash - 1.0) * 100.0)

    return BacktestResult(
        initial_cash=initial_cash,
        final_equity=final_eq,
        total_return_pct=tot_ret,
        daily_snapshots=snapshots,
        trades=trades,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )
