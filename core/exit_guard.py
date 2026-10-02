# core/exit_guard.py
"""개별 포지션 손절/트레일링익절 판단. 순수 함수 — I/O 없음, 주문 생성도 안 한다
(core/rebalancer.py와 동일한 설계: "결정"과 "주문으로 변환"을 분리).

signal_engine.decide()와는 별개의, 의도적으로 우회하는 경로다 — 밴드/시그널/쿨다운과
협상하지 않는다. 손절은 손실을 줄이는 강제청산이므로 신규 재량매매를 막기 위한
쿨다운/일일손실한도 게이트보다 먼저(그리고 무관하게) 평가되어야 한다.
"""
from dataclasses import dataclass
from typing import Optional

from core.config import settings


@dataclass
class ExitDecision:
    reason: str  # "STOP_LOSS" | "TRAILING_TAKE_PROFIT"
    sell_ratio: float = 1.0  # 1.0: 전량 매도, 0.5: 50% 분할 매도 (상승 추세 시 랠리 완주용)


def trailing_params(atr_pct: Optional[float], is_uptrend: bool = False) -> tuple[float, float]:
    """(트레일링 폭, 무장 기준 수익률). ATR%가 있으면 종목 변동성에 비례하고, 없으면 고정값 폴백.
    
    상승 추세(is_uptrend=True)에서는 일간 노이즈에 조기 청산되지 않도록 폭과 무장 기준을 1.5배 여유 있게 확장합니다.
    """
    if atr_pct is None or atr_pct <= 0:
        base_trail, base_arm = settings.TRAILING_TAKE_PROFIT_PCT, settings.TRAILING_ARM_FALLBACK_PCT
    else:
        base_trail = min(max(settings.TRAILING_ATR_MULT * atr_pct, settings.TRAILING_MIN_PCT), settings.TRAILING_MAX_PCT)
        base_arm = settings.TRAILING_ARM_ATR_MULT * atr_pct

    if is_uptrend:
        return max(base_trail * 1.5, 0.06), max(base_arm * 1.5, 0.08)
    return base_trail, base_arm


def evaluate_exit(
    avg_price: float,
    peak_price: Optional[float],
    current_price: float,
    atr_pct: Optional[float] = None,
    is_uptrend: bool = False,
) -> Optional[ExitDecision]:
    if avg_price <= 0 or current_price <= 0:
        return None

    # 1. 손절: 평단 대비 -7% 도달 시 전량(100%) 강제 청산
    if (current_price - avg_price) / avg_price <= -settings.STOP_LOSS_PCT:
        return ExitDecision(reason="STOP_LOSS", sell_ratio=1.0)

    # 2. 트레일링 익절: 고점이 평단 대비 무장 기준 이상 오른 뒤 고점 대비 트레일링 폭만큼 하락 시 발동.
    # 상승 추세에서는 전량 청산이 아닌 50% 분할 익절(sell_ratio=0.5)을 실행하여 나머지 수량으로 추세를 유지합니다.
    trail_pct, arm_pct = trailing_params(atr_pct, is_uptrend=is_uptrend)
    peak = max(peak_price or avg_price, avg_price)
    if (peak - avg_price) / avg_price >= arm_pct and (current_price - peak) / peak <= -trail_pct:
        sell_ratio = 0.5 if is_uptrend else 1.0
        return ExitDecision(reason="TRAILING_TAKE_PROFIT", sell_ratio=sell_ratio)

    return None
