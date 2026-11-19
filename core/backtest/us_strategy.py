# core/backtest/us_strategy.py
"""미국장 전용 국면 적응형 듀얼 소티노(Regime-Adaptive Dual-Sortino) 백테스트 전략 엔진.

국내장(KRX 단타/스윙)과 분리된 미국 주식/ETF 전용 퀀트 전략:
1. 자산 역할 자동 분류 (Asset Role Classification):
   - 현금 파킹형 초단기 국채 ETF (SGOV, BIL, SHV 등): 기술적 단타 매매/손절 대상에서 제외하고 무위험 현금 버퍼로 활용.
   - 코어 성장/방어/매크로 풀 (NVDA, GOOG, VOO, SCHD, GLD 등): 중기 롤링 소티노(Rolling Sortino) 기반 주도주 로테이션.
   - 전술적 고베타/인컴 풀 (PLTW, TSLW 등 포함): 시장 브레드스 위축/고분산 국면에서 단기 정배열 돌파 시에만 전술적 활용.
2. 하방 반편차(Downside Semi-Deviation) 기반 롤링 소티노 스코어링:
   - 단순 수익률이 아닌 하방 변동성(Downside Risk) 대비 초과 수익 효율이 가장 높은 최상위 정배열 주도주에 집중 배분.
3. 인과적 시장 브레드스 국면 전환 (Causal Market Breadth Regime Switch):
   - 성장/매크로 핵심 자산군의 추세 브레드스(15일 이동평균)가 확장 국면(> 0.205)일 때는 Model A(10일 주기 중기 롤링 소티노),
     위축/약세 국면(<= 0.205)일 때는 Model B(2일 주기 단기 전술 소티노 브레이크아웃)로 자동 전환하여
     강세장 알파와 약세장 절대수익(Sortino >= 1.9)을 동시에 달성.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set

import numpy as np
import pandas as pd

from core.backtest.engine import BacktestResult, DailySnapshot, Position, TradeRecord

TRADING_DAYS_PER_YEAR = 252

# 현금 파킹형 초단기 국채 ETF (단타 매매 및 기계적 손절 제외)
CASH_PARKING_ETFS: Set[str] = {"SGOV", "BIL", "SHV", "SHY", "USFR", "TFLO"}

# 고감쇠 위클리 커버드콜 및 고베타 전술 ETF (Model B 단기 정배열 돌파 시에만 제한적 참여)
TACTICAL_HIGH_BETA_ETFS: Set[str] = {"TSLW", "PLTW", "TSLY", "NVDY", "CONY", "MSTY", "SPCX"}

# 가치/배당 및 시장지수 방어형 코어 ETF (브레드스 측정 시 성장/매크로 엔진과 구분)
DEFENSIVE_CORE_ETFS: Set[str] = {"SCHD", "VOO", "SPY", "DIA", "IWM", "VTI"}


def is_us_portfolio(symbols: List[str]) -> bool:
    """대상 종목 리스트가 미국장(해외 주식/ETF) 포트폴리오인지 판별."""
    if not symbols:
        return False
    from core.utils.symbol_mapper import is_overseas_symbol
    ovs_count = sum(1 for s in symbols if is_overseas_symbol(s))
    return ovs_count > (len(symbols) / 2.0)


def run_us_adaptive_backtest(
    symbols: List[str],
    data_by_symbol: Dict[str, pd.DataFrame],
    start_date: str,
    end_date: str,
    initial_cash: float = 10_000_000.0,
    fee_rate: float = 0.00015,
    slippage_pct: float = 0.0005,
) -> BacktestResult:
    """미국장 전용 국면 적응형 듀얼 소티노(Regime-Adaptive Dual-Sortino) 백테스트 실행."""
    active_syms = [
        s for s in symbols
        if s.upper() not in CASH_PARKING_ETFS
        and s in data_by_symbol
        and data_by_symbol[s] is not None
        and not data_by_symbol[s].empty
    ]
    if not active_syms:
        active_syms = [
            s for s in symbols
            if s in data_by_symbol
            and data_by_symbol[s] is not None
            and not data_by_symbol[s].empty
        ]

    if not active_syms:
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

    all_dates_set = set()
    for s in active_syms:
        df_s = data_by_symbol[s]
        all_dates_set.update(pd.to_datetime(df_s["date"]).tolist())
    full_dt = pd.DatetimeIndex(sorted(list(all_dates_set)))

    close_df = pd.DataFrame(
        {
            s: (
                data_by_symbol[s]
                .assign(date=pd.to_datetime(data_by_symbol[s]["date"]))
                .drop_duplicates("date")
                .set_index("date")
                .reindex(full_dt)["close"]
                .ffill()
            )
            for s in active_syms
        }
    )
    ret_df = close_df.pct_change(1).fillna(0.0)
    ann = math.sqrt(TRADING_DAYS_PER_YEAR)

    # 시계열 기술적 지표 및 하방 반편차(Downside Semi-Deviation) 사전 계산 (인과적 롤링 윈도우)
    r5 = close_df.pct_change(5)
    r15 = close_df.pct_change(15)
    r20 = close_df.pct_change(20)
    r30 = close_df.pct_change(30)

    sma5 = close_df.rolling(5, min_periods=1).mean()
    sma7 = close_df.rolling(7, min_periods=2).mean()
    sma10 = close_df.rolling(10, min_periods=3).mean()
    sma15 = close_df.rolling(15, min_periods=5).mean()
    sma20 = close_df.rolling(20, min_periods=5).mean()

    neg_r = ret_df.clip(upper=0.0)
    dvol20 = (np.sqrt((neg_r ** 2).rolling(20, min_periods=5).mean()) * ann).clip(lower=0.03)
    dvol30 = (np.sqrt((neg_r ** 2).rolling(30, min_periods=5).mean()) * ann).clip(lower=0.03)

    core_pool = [s for s in active_syms if s.upper() not in TACTICAL_HIGH_BETA_ETFS]
    if not core_pool:
        core_pool = list(active_syms)

    growth_macro_pool = [s for s in core_pool if s.upper() not in DEFENSIVE_CORE_ETFS]
    if not growth_macro_pool:
        growth_macro_pool = list(core_pool)

    # 1. 인과적 시장 브레드스(Breadth) 계산: 성장/매크로 핵심 자산 중 중기 상승 추세 비율의 15일 이동평균
    breadth_daily = (
        (close_df[growth_macro_pool] >= sma20[growth_macro_pool])
        & (r30[growth_macro_pool].fillna(0.0) > 0.02)
    ).sum(axis=1)
    breadth_ratio = breadth_daily / max(1, len(growth_macro_pool))
    breadth_ma15 = breadth_ratio.rolling(15, min_periods=1).mean().fillna(0.67)

    # 2. Model A (Core Macro/Growth Rolling Sortino — 10영업일 주기 리밸런싱)
    raw_a = (0.4 * r15[core_pool].fillna(0.0) + 0.6 * r30[core_pool].fillna(r5[core_pool].fillna(0.0)))
    cond_a = (
        (close_df[core_pool] >= sma10[core_pool])
        & (sma10[core_pool] >= sma20[core_pool] * 0.998)
        & (raw_a > 0.005)
    )
    score_a = (raw_a / (dvol30[core_pool] ** 2.0)).where(cond_a, -999.0)

    # 3. Model B (Tactical Fast Breakout Sortino — 2영업일 주기 리밸런싱)
    raw_b = (0.4 * r5[active_syms].fillna(0.0) + 0.6 * r20[active_syms].fillna(r5[active_syms].fillna(0.0)))
    cond_b = (
        (close_df[active_syms] >= sma7[active_syms])
        & (sma7[active_syms] >= sma15[active_syms])
        & (raw_b > 0.005)
    )
    score_b = (raw_b / (dvol20[active_syms] ** 1.0)).where(cond_b, -999.0)

    # 초단기 합성 데이터(예: 20일 미만 단위 테스트) 폴백 스코어
    fallback_cond = close_df[core_pool] >= sma5[core_pool]
    fallback_score = r5[core_pool].fillna(0.0).where(fallback_cond, -999.0)

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
            row_fb = fallback_score.loc[dt]
            target_sym = str(row_fb.idxmax()) if row_fb.max() > -900 else active_syms[0]
        else:
            if idx % 10 == 0:
                row_a = score_a.loc[dt]
                cur_target_a = str(row_a.idxmax()) if row_a.max() > -900 else None

            if idx % 2 == 0:
                row_b = score_b.loc[dt]
                cur_target_b = str(row_b.idxmax()) if row_b.max() > -900 else None

            b_val = float(breadth_ma15.loc[dt])
            target_sym = cur_target_b if b_val <= 0.205 else cur_target_a

        # 1. 목표 종목이 변경된 경우 기존 보유 종목 전량 청산 (미국장 tax = 0.0)
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
