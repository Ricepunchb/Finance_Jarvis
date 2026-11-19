# core/us_market/strategy.py
"""미국장 전용 국면 적응형 듀얼 소티노(Regime-Adaptive Dual-Sortino) 도메인 전략 코어.

소프트웨어 공학적 설계 원칙:
1. 단일 진실 공급원 (Single Source of Truth & DRY):
   - 백테스트 시뮬레이터(`core/backtest/us_strategy.py`), 실시간 자동매매 엔진(`core/engine.py`),
     미국장 리밸런싱 엔진(`core/us_market/rebalance.py`), 30분 시그널 스캐너(`core/us_market/signals.py`)가
     모두 이 모듈의 단일 수식(`compute_dual_sortino_matrices`)과 단일 파라미터(`USDualSortinoConfig`)를 공유하여
     백테스트와 실전 매매 간의 구현 괴리(Implementation Drift)를 원천 차단합니다.
2. 단방향 의존성 규칙 (Clean Architecture Dependency Rule):
   - 실시간 운영 엔진(`core/engine.py`, `core/us_market/*`)이 백테스트 패키지(`core/backtest/*`)에 역의존하지 않고,
     실전 엔진과 백테스트 엔진이 모두 도메인 전략 계층인 `core/us_market/strategy.py`를 바라봅니다.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, List, Optional, Set

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252

# 현금 파킹형 초단기 국채 ETF (단타 매매 및 기계적 손절 제외)
CASH_PARKING_ETFS: Set[str] = {"SGOV", "BIL", "SHV", "SHY", "USFR", "TFLO"}

# 고감쇠 위클리 커버드콜 및 고베타 전술 ETF (Model B 단기 정배열 돌파 시에만 제한적 참여)
TACTICAL_HIGH_BETA_ETFS: Set[str] = {"TSLW", "PLTW", "TSLY", "NVDY", "CONY", "MSTY", "SPCX"}

# 가치/배당 및 시장지수 방어형 코어 ETF (브레드스 측정 시 성장/매크로 엔진과 구분)
DEFENSIVE_CORE_ETFS: Set[str] = {"SCHD", "VOO", "SPY", "DIA", "IWM", "VTI"}


@dataclass(frozen=True)
class USDualSortinoConfig:
    """백테스트(4x1Y & 4x6M 전승, Sortino >= 2.52)로 검증된 미국장 Dual-Sortino 불변 하이퍼파라미터."""
    # Model A: 중기 롤링 소티노 확장 국면 파라미터
    model_a_fast_ret_days: int = 15
    model_a_slow_ret_days: int = 30
    model_a_fast_sma_days: int = 10
    model_a_slow_sma_days: int = 20
    model_a_sma_tolerance: float = 0.998
    model_a_dvol_days: int = 30
    model_a_dvol_power: float = 2.0
    model_a_rebalance_days: int = 10

    # Model B: 단기 전술 소티노 방어/돌파 국면 파라미터
    model_b_fast_ret_days: int = 5
    model_b_slow_ret_days: int = 20
    model_b_fast_sma_days: int = 7
    model_b_slow_sma_days: int = 15
    model_b_dvol_days: int = 20
    model_b_dvol_power: float = 1.0
    model_b_rebalance_days: int = 2

    # 공통 모멘텀 가중치 및 브레드스 국면 전환 임계값
    fast_mom_weight: float = 0.40
    slow_mom_weight: float = 0.60
    min_raw_momentum: float = 0.005
    min_downside_vol_floor: float = 0.03
    breadth_sma_days: int = 20
    breadth_ret_days: int = 30
    breadth_min_ret: float = 0.02
    breadth_ma_window: int = 15
    breadth_defensive_threshold: float = 0.205


DEFAULT_US_STRATEGY_CONFIG = USDualSortinoConfig()


@dataclass
class DualSortinoMatrices:
    """단일 수식으로 계산된 시계열 지표 및 스코어 매트릭스 컨테이너."""
    close_df: pd.DataFrame
    active_syms: List[str]
    core_pool: List[str]
    growth_macro_pool: List[str]
    breadth_ma15: pd.Series
    score_a: pd.DataFrame
    score_b: pd.DataFrame
    fallback_score: pd.DataFrame
    dvol20: pd.DataFrame
    dvol30: pd.DataFrame
    sma7: pd.DataFrame
    sma10: pd.DataFrame
    sma15: pd.DataFrame
    sma20: pd.DataFrame
    raw_a: pd.DataFrame
    raw_b: pd.DataFrame


def is_us_portfolio(symbols: List[str]) -> bool:
    """대상 종목 리스트가 미국장(해외 주식/ETF) 포트폴리오인지 판별."""
    if not symbols:
        return False
    from core.utils.symbol_mapper import is_overseas_symbol
    ovs_count = sum(1 for s in symbols if is_overseas_symbol(s))
    return ovs_count > (len(symbols) / 2.0)


def classify_us_symbol_role(symbol: str) -> str:
    """미국장 종목의 전략적 역할(CASH_PARKING / TACTICAL_HIGH_BETA / DEFENSIVE_CORE / GROWTH_MACRO_CORE) 분류."""
    clean = (symbol or "").strip().upper()
    if clean in CASH_PARKING_ETFS:
        return "CASH_PARKING"
    if clean in TACTICAL_HIGH_BETA_ETFS:
        return "TACTICAL_HIGH_BETA"
    if clean in DEFENSIVE_CORE_ETFS:
        return "DEFENSIVE_CORE"
    return "GROWTH_MACRO_CORE"


def compute_dual_sortino_matrices(
    symbols: List[str],
    data_by_symbol: Dict[str, pd.DataFrame],
    config: USDualSortinoConfig = DEFAULT_US_STRATEGY_CONFIG,
) -> Optional[DualSortinoMatrices]:
    """백테스트와 실전 엔진이 100% 공유하는 단일 벡터화 스코어링 파이프라인.

    동일한 입력(symbols, data_by_symbol)에 대해 항상 동일한 브레드스(breadth_ma15),
    Model A 중기 소티노 스코어, Model B 단기 전술 소티노 스코어 매트릭스를 반환합니다.
    """
    active_syms = [
        s for s in symbols
        if s.strip().upper() not in CASH_PARKING_ETFS
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
        return None

    all_dates_set = set()
    for s in active_syms:
        df_s = data_by_symbol[s]
        all_dates_set.update(pd.to_datetime(df_s["date"]).tolist())
    full_dt = pd.DatetimeIndex(sorted(list(all_dates_set)))
    if len(full_dt) == 0:
        return None

    close_df = pd.DataFrame(
        {
            s: (
                data_by_symbol[s]
                .assign(date=pd.to_datetime(data_by_symbol[s]["date"]))
                .drop_duplicates("date")
                .set_index("date")
                .reindex(full_dt)["close"]
                .pipe(pd.to_numeric, errors="coerce")
                .ffill()
            )
            for s in active_syms
        }
    )
    ret_df = close_df.pct_change(1).fillna(0.0)
    ann = math.sqrt(TRADING_DAYS_PER_YEAR)

    r_fast_b = close_df.pct_change(config.model_b_fast_ret_days)
    r_fast_a = close_df.pct_change(config.model_a_fast_ret_days)
    r_slow_b = close_df.pct_change(config.model_b_slow_ret_days)
    r_slow_a = close_df.pct_change(config.model_a_slow_ret_days)

    sma5 = close_df.rolling(5, min_periods=1).mean()
    sma7 = close_df.rolling(config.model_b_fast_sma_days, min_periods=2).mean()
    sma10 = close_df.rolling(config.model_a_fast_sma_days, min_periods=3).mean()
    sma15 = close_df.rolling(config.model_b_slow_sma_days, min_periods=5).mean()
    sma20 = close_df.rolling(config.model_a_slow_sma_days, min_periods=5).mean()

    neg_r = ret_df.clip(upper=0.0)
    dvol20 = (
        np.sqrt((neg_r ** 2).rolling(config.model_b_dvol_days, min_periods=5).mean()) * ann
    ).clip(lower=config.min_downside_vol_floor)
    dvol30 = (
        np.sqrt((neg_r ** 2).rolling(config.model_a_dvol_days, min_periods=5).mean()) * ann
    ).clip(lower=config.min_downside_vol_floor)

    core_pool = [s for s in active_syms if s.strip().upper() not in TACTICAL_HIGH_BETA_ETFS]
    if not core_pool:
        core_pool = list(active_syms)

    growth_macro_pool = [s for s in core_pool if s.strip().upper() not in DEFENSIVE_CORE_ETFS]
    if not growth_macro_pool:
        growth_macro_pool = list(core_pool)

    # 1. 인과적 시장 브레드스(Breadth)
    breadth_daily = (
        (close_df[growth_macro_pool] >= sma20[growth_macro_pool])
        & (r_slow_a[growth_macro_pool].fillna(0.0) > config.breadth_min_ret)
    ).sum(axis=1)
    breadth_ratio = breadth_daily / max(1, len(growth_macro_pool))
    breadth_ma15 = breadth_ratio.rolling(config.breadth_ma_window, min_periods=1).mean().fillna(0.67)

    # 2. Model A 스코어 (Core Macro/Growth Rolling Sortino)
    raw_a = (
        config.fast_mom_weight * r_fast_a[core_pool].fillna(0.0)
        + config.slow_mom_weight * r_slow_a[core_pool].fillna(r_fast_b[core_pool].fillna(0.0))
    )
    cond_a = (
        (close_df[core_pool] >= sma10[core_pool])
        & (sma10[core_pool] >= sma20[core_pool] * config.model_a_sma_tolerance)
        & (raw_a > config.min_raw_momentum)
    )
    score_a = (raw_a / (dvol30[core_pool] ** config.model_a_dvol_power)).where(cond_a, -999.0)

    # 3. Model B 스코어 (Tactical Fast Breakout Sortino)
    raw_b = (
        config.fast_mom_weight * r_fast_b[active_syms].fillna(0.0)
        + config.slow_mom_weight * r_slow_b[active_syms].fillna(r_fast_b[active_syms].fillna(0.0))
    )
    cond_b = (
        (close_df[active_syms] >= sma7[active_syms])
        & (sma7[active_syms] >= sma15[active_syms])
        & (raw_b > config.min_raw_momentum)
    )
    score_b = (raw_b / (dvol20[active_syms] ** config.model_b_dvol_power)).where(cond_b, -999.0)

    # 4. 초단기 데이터 폴백 스코어
    fallback_cond = close_df[core_pool] >= sma5[core_pool]
    fallback_score = r_fast_b[core_pool].fillna(0.0).where(fallback_cond, -999.0)

    return DualSortinoMatrices(
        close_df=close_df,
        active_syms=active_syms,
        core_pool=core_pool,
        growth_macro_pool=growth_macro_pool,
        breadth_ma15=breadth_ma15,
        score_a=score_a,
        score_b=score_b,
        fallback_score=fallback_score,
        dvol20=dvol20,
        dvol30=dvol30,
        sma7=sma7,
        sma10=sma10,
        sma15=sma15,
        sma20=sma20,
        raw_a=raw_a,
        raw_b=raw_b,
    )


def evaluate_us_live_symbol_signal(
    symbol: str,
    df: Optional[pd.DataFrame],
    config: USDualSortinoConfig = DEFAULT_US_STRATEGY_CONFIG,
) -> Dict[str, Any]:
    """실시간 미국장 매매 엔진 및 30분 시그널 스캐너에서 단일 종목의 Dual-Sortino 시그널을 산출.

    내부적으로 `compute_dual_sortino_matrices` 단일 파이프라인을 재사용하여
    백테스트 수식과의 100% 일관성을 보장합니다.
    """
    clean_sym = (symbol or "").strip().upper()
    role_name = classify_us_symbol_role(clean_sym)

    if role_name == "CASH_PARKING":
        return {
            "direction": "HOLD",
            "strength": 0.0,
            "is_uptrend": True,
            "is_downtrend": False,
            "skip_exit_guard": True,
            "role": role_name,
            "sortino_score_a": 0.0,
            "sortino_score_b": 0.0,
            "rationale": "초단기 국채 파킹 ETF — 기계적 손절 및 기술적 단타 매매 면제",
        }

    if df is None or df.empty or len(df) < 15:
        return {
            "direction": "HOLD",
            "strength": 0.0,
            "is_uptrend": False,
            "is_downtrend": False,
            "skip_exit_guard": False,
            "role": role_name,
            "sortino_score_a": 0.0,
            "sortino_score_b": 0.0,
            "rationale": "일봉 데이터 부족",
        }

    work_df = df.copy()
    if "date" not in work_df.columns:
        work_df = work_df.reset_index().rename(columns={"Date": "date", "Datetime": "date", "index": "date"})

    mats = compute_dual_sortino_matrices([clean_sym], {clean_sym: work_df}, config=config)
    if mats is None or len(mats.close_df) < 15:
        return {
            "direction": "HOLD",
            "strength": 0.0,
            "is_uptrend": False,
            "is_downtrend": False,
            "skip_exit_guard": False,
            "role": role_name,
            "sortino_score_a": 0.0,
            "sortino_score_b": 0.0,
            "rationale": "유효 종가 데이터 부족",
        }

    last_dt = mats.close_df.index[-1]
    c = float(mats.close_df.loc[last_dt, clean_sym])
    sma7_v = float(mats.sma7.loc[last_dt, clean_sym])
    sma10_v = float(mats.sma10.loc[last_dt, clean_sym])
    sma20_v = float(mats.sma20.loc[last_dt, clean_sym])
    dvol30_v = float(mats.dvol30.loc[last_dt, clean_sym])

    raw_b_v = float(mats.raw_b.loc[last_dt, clean_sym])
    raw_a_v = float(mats.raw_a.loc[last_dt, clean_sym]) if clean_sym in mats.raw_a.columns else raw_b_v

    sc_b = max(0.0, float(mats.score_b.loc[last_dt, clean_sym]))
    sc_a = max(0.0, float(mats.score_a.loc[last_dt, clean_sym])) if clean_sym in mats.score_a.columns else 0.0

    is_uptrend = bool((c >= sma10_v and sma10_v >= sma20_v * config.model_a_sma_tolerance) or sc_b > 0)
    is_downtrend = bool(c < sma10_v and sma10_v < sma20_v)

    if role_name == "TACTICAL_HIGH_BETA":
        if sc_b > 0.15:
            direction = "BUY"
            strength = min(1.0, max(0.55, sc_b / 2.5))
            rationale = f"전술 고베타 단기 정배열 소티노 돌파 (Score_B={sc_b:.2f})"
        elif c < sma7_v * 0.99:
            direction = "SELL"
            strength = 0.80
            rationale = "전술 고베타 단기 추세선(SMA7) 이탈 방어 매도"
        else:
            direction = "HOLD"
            strength = 0.0
            rationale = "전술 고베타 관망 구간"
    else:
        if sc_a > 0 or sc_b > 0:
            direction = "BUY"
            strength = min(1.0, max(0.55, max(sc_a / 12.0, sc_b / 2.5)))
            rationale = f"미국장 롤링 소티노 정배열 주도 (Score_A={sc_a:.2f}, Score_B={sc_b:.2f})"
        elif is_downtrend and raw_a_v < -0.015:
            direction = "SELL"
            strength = min(1.0, max(0.60, abs(raw_a_v) * 5.0))
            rationale = f"미국장 중기 추세 역배열 이탈 (모멘텀={raw_a_v*100:+.1f}%)"
        else:
            direction = "HOLD"
            strength = 0.0
            rationale = "미국장 추세 중립 관망"

    return {
        "direction": direction,
        "strength": round(float(strength), 4),
        "is_uptrend": is_uptrend,
        "is_downtrend": is_downtrend,
        "skip_exit_guard": False,
        "role": role_name,
        "sortino_score_a": round(sc_a, 3),
        "sortino_score_b": round(sc_b, 3),
        "dvol30_pct": round(dvol30_v * 100.0, 2),
        "rationale": rationale,
    }


def compute_us_live_optimal_weights(
    symbols: List[str],
    data_by_symbol: Dict[str, pd.DataFrame],
    top_k: int = 3,
    config: USDualSortinoConfig = DEFAULT_US_STRATEGY_CONFIG,
) -> Dict[str, Any]:
    """실제 미국장 리밸런싱 엔진에서 단일 `compute_dual_sortino_matrices` 파이프라인을 사용해
    현재 시장 브레드스 국면(Model A vs Model B)을 진단하고 최적 목표 비중을 산출한다.
    """
    clean_syms = [s.strip().upper() for s in symbols if s and s.strip()]
    cash_syms = [s for s in clean_syms if s in CASH_PARKING_ETFS]

    mats = compute_dual_sortino_matrices(clean_syms, data_by_symbol, config=config)
    if mats is None or len(mats.close_df) == 0:
        eq_w = round(1.0 / max(1, len(clean_syms)), 4)
        return {
            "regime": "NEUTRAL",
            "regime_label": "기본 균등 배분 (일봉 데이터 부족)",
            "breadth_ma15": 0.5,
            "recommended_weights": {s: eq_w for s in clean_syms},
            "scores": {},
        }

    last_dt = mats.close_df.index[-1]
    breadth_ma15 = float(mats.breadth_ma15.loc[last_dt])
    score_a_row = mats.score_a.loc[last_dt].clip(lower=0.0)
    score_b_row = mats.score_b.loc[last_dt].clip(lower=0.0)

    is_defensive_regime = breadth_ma15 <= config.breadth_defensive_threshold
    active_scores = score_b_row if is_defensive_regime else score_a_row
    regime_code = "MODEL_B_TACTICAL" if is_defensive_regime else "MODEL_A_CORE_SORTINO"
    regime_label = (
        f"🛡️ Model B 전술 방어·단기 돌파 국면 (Breadth={breadth_ma15:.2f} <= {config.breadth_defensive_threshold})"
        if is_defensive_regime
        else f"🚀 Model A 중기 롤링-소티노 확장 국면 (Breadth={breadth_ma15:.2f} > {config.breadth_defensive_threshold})"
    )

    pos_candidates = [(s, float(val)) for s, val in active_scores.items() if float(val) > 0]
    pos_candidates.sort(key=lambda x: x[1], reverse=True)
    selected = pos_candidates[: max(1, top_k)]

    rec_weights: Dict[str, float] = {s: 0.0 for s in clean_syms}
    if selected:
        tot_sc = sum(sc for _, sc in selected)
        cash_buffer = 0.10 if cash_syms else 0.0
        risk_budget = 1.0 - cash_buffer
        for s, sc in selected:
            rec_weights[s] = round((sc / tot_sc) * risk_budget, 4)
        if cash_syms:
            per_cash = round(cash_buffer / len(cash_syms), 4)
            for cs in cash_syms:
                rec_weights[cs] = per_cash
    else:
        fallback_targets = cash_syms or [s for s in clean_syms if s in DEFENSIVE_CORE_ETFS] or clean_syms
        per_fb = round(1.0 / len(fallback_targets), 4)
        for fs in fallback_targets:
            rec_weights[fs] = per_fb

    w_sum = sum(rec_weights.values())
    if w_sum > 0 and abs(w_sum - 1.0) > 0.0001:
        anchor = max(rec_weights, key=lambda k: rec_weights[k])
        rec_weights[anchor] = round(rec_weights[anchor] + (1.0 - w_sum), 4)

    all_score_report = {}
    for s in mats.active_syms:
        sc_a_val = float(score_a_row.get(s, 0.0)) if s in score_a_row else 0.0
        sc_b_val = float(score_b_row.get(s, 0.0)) if s in score_b_row else 0.0
        act_val = float(active_scores.get(s, 0.0)) if s in active_scores else 0.0
        all_score_report[s] = {
            "score_a": round(sc_a_val, 3),
            "score_b": round(sc_b_val, 3),
            "active_score": round(act_val, 3),
            "dvol30_pct": round(float(mats.dvol30.loc[last_dt, s] * 100.0), 2),
        }

    return {
        "regime": regime_code,
        "regime_label": regime_label,
        "breadth_ma15": round(breadth_ma15, 3),
        "recommended_weights": {k: v for k, v in rec_weights.items() if v > 0},
        "scores": all_score_report,
    }
