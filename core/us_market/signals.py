# core/us_market/signals.py
"""미국 주식 30분봉·일봉 결합 시그널 및 손절/트레일링 익절 감시 모듈.

Finance Jarvis의 기존 퀀트 알고리즘(RSI/MACD/볼린저밴드/추세국면 결합 및 ATR Exit Guard)을
미국 주식 및 ETF에 최적화하여 적용합니다.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import pandas_ta as ta

from core.config import settings
from core.indicators import compute_atr_pct, compute_technical_signal
from core.signal_engine import combine_signals

logger = logging.getLogger(__name__)


def extract_indicator_snapshot(df: pd.DataFrame) -> Dict[str, Any]:
    """DataFrame의 최신 지표(RSI, MACD, 볼린저 밴드, 이평선) 스냅샷 추출."""
    if df.empty or len(df) < 20:
        return {}

    work = df.copy()
    try:
        work.ta.rsi(length=14, append=True)
        work.ta.macd(fast=12, slow=26, signal=9, append=True)
        work.ta.bbands(length=20, std=2, append=True)

        last = work.iloc[-1]
        close = float(last["close"])

        # RSI
        rsi_col = [c for c in work.columns if c.startswith("RSI_")]
        rsi_val = float(last[rsi_col[0]]) if rsi_col and pd.notna(last[rsi_col[0]]) else None

        # MACD
        macd_col = [c for c in work.columns if c.startswith("MACD_") and not c.startswith("MACDh_") and not c.startswith("MACDs_")]
        sig_col = [c for c in work.columns if c.startswith("MACDs_")]
        hist_col = [c for c in work.columns if c.startswith("MACDh_")]

        macd_val = float(last[macd_col[0]]) if macd_col and pd.notna(last[macd_col[0]]) else None
        macds_val = float(last[sig_col[0]]) if sig_col and pd.notna(last[sig_col[0]]) else None
        macdh_val = float(last[hist_col[0]]) if hist_col and pd.notna(last[hist_col[0]]) else None

        # 볼린저 밴드
        bbl_col = [c for c in work.columns if c.startswith("BBL_")]
        bbm_col = [c for c in work.columns if c.startswith("BBM_")]
        bbu_col = [c for c in work.columns if c.startswith("BBU_")]

        bbl_val = float(last[bbl_col[0]]) if bbl_col and pd.notna(last[bbl_col[0]]) else None
        bbm_val = float(last[bbm_col[0]]) if bbm_col and pd.notna(last[bbm_col[0]]) else None
        bbu_val = float(last[bbu_col[0]]) if bbu_col and pd.notna(last[bbu_col[0]]) else None

        # 이동평균
        sma20 = float(work["close"].rolling(20).mean().iloc[-1]) if len(work) >= 20 else None
        sma60 = float(work["close"].rolling(60).mean().iloc[-1]) if len(work) >= 60 else None

        return {
            "close": close,
            "rsi": round(rsi_val, 1) if rsi_val is not None else None,
            "macd": round(macd_val, 3) if macd_val is not None else None,
            "macd_signal": round(macds_val, 3) if macds_val is not None else None,
            "macd_hist": round(macdh_val, 3) if macdh_val is not None else None,
            "bb_lower": round(bbl_val, 2) if bbl_val is not None else None,
            "bb_middle": round(bbm_val, 2) if bbm_val is not None else None,
            "bb_upper": round(bbu_val, 2) if bbu_val is not None else None,
            "sma20": round(sma20, 2) if sma20 is not None else None,
            "sma60": round(sma60, 2) if sma60 is not None else None,
        }
    except Exception as e:
        logger.warning(f"지표 스냅샷 산출 예외: {e}")
        return {}


def evaluate_us_exit_guard(
    current_price: float,
    avg_entry_price: Optional[float],
    peak_price: Optional[float],
    atr_pct: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """보유자의 손절 및 트레일링 익절 조건을 평가한다.

    1. 손절: 평단가 대비 -6%(설정값) 도달 시 즉시 발동
    2. 트레일링 익절: 진입 후 고점(+3% 이상) 대비 하락 시(기본 -5% 또는 ATR 기반) 발동
    """
    if not avg_entry_price or avg_entry_price <= 0 or current_price <= 0:
        return None

    pnl_pct = (current_price - avg_entry_price) / avg_entry_price
    effective_peak = max(peak_price or avg_entry_price, avg_entry_price, current_price)

    # 1. 손절 검사
    stop_threshold = -abs(settings.STOP_LOSS_PCT)
    if pnl_pct <= stop_threshold:
        return {
            "trigger": "STOP_LOSS",
            "pnl_pct": round(pnl_pct * 100, 2),
            "reason": (
                f"🚨 [손절 경고] 평단가(${avg_entry_price:.2f}) 대비 "
                f"{pnl_pct * 100:.1f}% 하락 (손절 기준 {stop_threshold * 100:.0f}% 도달)"
            ),
        }

    # 2. 트레일링 익절 검사
    # 고점이 평단가보다 일정 수준(+3% 또는 ATR 배수) 이상 상승했었는지 확인
    arm_pct = settings.TRAILING_ARM_FALLBACK_PCT
    if effective_peak >= avg_entry_price * (1.0 + arm_pct):
        # 트레일링 폭 결정 (ATR 기반 또는 기본 5%)
        trail_pct = settings.TRAILING_TAKE_PROFIT_PCT
        if atr_pct:
            dynamic_trail = min(max(atr_pct * settings.TRAILING_ATR_MULT, settings.TRAILING_MIN_PCT), settings.TRAILING_MAX_PCT)
            trail_pct = dynamic_trail

        drop_from_peak = (current_price - effective_peak) / effective_peak
        if drop_from_peak <= -trail_pct:
            return {
                "trigger": "TRAILING_TAKE_PROFIT",
                "pnl_pct": round(pnl_pct * 100, 2),
                "drop_from_peak": round(drop_from_peak * 100, 2),
                "peak_price": effective_peak,
                "reason": (
                    f"🎯 [트레일링 익절 권고] 최고가(${effective_peak:.2f}) 대비 "
                    f"{drop_from_peak * 100:.1f}% 하락 (수익 보존 익절 타이밍, 평단 대비 +{pnl_pct * 100:.1f}%)"
                ),
            }

    return None


def compute_us_signals(
    symbol: str,
    df_30m: pd.DataFrame,
    df_daily: pd.DataFrame,
    current_price: float,
    holding: Optional[Dict[str, Any]] = None,
    consensus: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """미국 종목의 30분봉 및 일봉 지표, 월가 컨센서스, 보유자 손절/익절을 종합 평가한다."""
    clean_sym = symbol.strip().upper()

    # 1. 일봉 기술적 지표 계산
    daily_signal = compute_technical_signal(df_daily)
    daily_snap = extract_indicator_snapshot(df_daily)

    # 2. 30분봉 기술적 지표 계산
    intraday_signal = compute_technical_signal(df_30m)
    intraday_snap = extract_indicator_snapshot(df_30m)

    # 3. 밸류에이션 / 월가 컨센서스 시그널
    valuation_signal = None
    if consensus and consensus.get("target_gap_pct") is not None:
        gap = consensus["target_gap_pct"]
        recomm = (consensus.get("recommendation_key") or "").lower()
        if gap >= 20.0 or "buy" in recomm:
            strength = min(1.0, gap / 50.0) if gap > 0 else 0.5
            valuation_signal = {"direction": "BUY", "strength": max(0.4, strength)}
        elif gap <= -5.0 or "underperform" in recomm or "sell" in recomm:
            strength = min(1.0, abs(gap) / 20.0)
            valuation_signal = {"direction": "SELL", "strength": max(0.4, strength)}
        else:
            valuation_signal = {"direction": "HOLD", "strength": 0.0}

    # 4. 결합 시그널 산출
    # combine_signals: tech(0.30) + intraday(0.50) + valuation(0.20)
    combined = combine_signals(
        tech=daily_signal,
        intraday=intraday_signal,
        sentiment=None,
        valuation=valuation_signal,
    )
    combined_action = combined.get("direction", "HOLD")
    strength = round(float(combined.get("strength", 0.0)), 2)

    # 5. Exit Guard (손절 / 트레일링 익절) 평가
    avg_price = holding.get("avg_entry_price") if holding else None
    peak_price = holding.get("peak_price") if holding else None
    atr_pct = compute_atr_pct(df_daily, settings.ATR_LENGTH) if not df_daily.empty else None

    exit_info = evaluate_us_exit_guard(current_price, avg_price, peak_price, atr_pct)

    # 6. 사용자 친화적 판단 근거 및 권고 행동 문안 작성
    if exit_info:
        exit_trigger = exit_info["trigger"]
        action_recommendation = exit_info["reason"]
        final_action = "SELL"
    else:
        exit_trigger = "NONE"
        final_action = combined_action

        # 지표 기반 서술형 근거 생성
        intra_dir = intraday_signal.get("direction", "HOLD")
        daily_dir = daily_signal.get("direction", "HOLD")
        rsi_30m = intraday_snap.get("rsi")
        rsi_daily = daily_snap.get("rsi")

        reasons = []
        if intra_dir == "BUY":
            reasons.append("30분봉 단기 모멘텀 상승")
            if rsi_30m and rsi_30m < 45:
                reasons.append(f"30분봉 눌림목 구간(RSI {rsi_30m:.0f})")
        elif intra_dir == "SELL":
            reasons.append("30분봉 단기 과열/하락 압력")
            if rsi_30m and rsi_30m > 70:
                reasons.append(f"30분봉 과매수 이탈(RSI {rsi_30m:.0f})")

        if daily_dir == "BUY":
            reasons.append("일봉 중기 상승 추세")
        elif daily_dir == "SELL":
            reasons.append("일봉 하락 추세 경계")

        if consensus and consensus.get("target_gap_pct") is not None:
            gap = consensus["target_gap_pct"]
            if gap > 15:
                reasons.append(f"월가 목표가 괴리율 +{gap:.1f}%")

        reason_text = ", ".join(reasons) if reasons else "단기 및 중기 지표 중립"

        if combined_action == "BUY":
            if strength >= 0.6:
                action_recommendation = f"🟢 [적극 매수] {reason_text} (시그널 강도 {int(strength*100)}%). 분할 매수 적기"
            else:
                action_recommendation = f"🟢 [매수 관심] {reason_text} (시그널 강도 {int(strength*100)}%). 지지선 확인 후 접근"
        elif combined_action == "SELL":
            if strength >= 0.6:
                action_recommendation = f"🔴 [비중 축소/매도] {reason_text} (시그널 강도 {int(strength*100)}%). 차익 실현 고려"
            else:
                action_recommendation = f"🔴 [분할 매도] {reason_text} (시그널 강도 {int(strength*100)}%). 리스크 관리"
        else:
            action_recommendation = f"🟡 [관망/보유] {reason_text}. 뚜렷한 진입 신호 대기"

    # 보유 포지션 손익률 계산
    pnl_pct = None
    if avg_price and avg_price > 0 and current_price > 0:
        pnl_pct = round(((current_price - avg_price) / avg_price) * 100, 2)

    return {
        "symbol": clean_sym,
        "price": current_price,
        "daily_signal": daily_signal,
        "intraday_signal": intraday_signal,
        "valuation_signal": valuation_signal,
        "combined_action": final_action,
        "strength": strength,
        "exit_trigger": exit_trigger,
        "action_recommendation": action_recommendation,
        "pnl_pct": pnl_pct,
        "daily_snapshot": daily_snap,
        "intraday_snapshot": intraday_snap,
    }

