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


def trailing_params(atr_pct: Optional[float]) -> tuple[float, float]:
    """(트레일링 폭, 무장 기준 수익률). ATR%가 있으면 종목 변동성에 비례하고, 없으면 고정값 폴백."""
    if atr_pct is None or atr_pct <= 0:
        return settings.TRAILING_TAKE_PROFIT_PCT, settings.TRAILING_ARM_FALLBACK_PCT
    trail = min(max(settings.TRAILING_ATR_MULT * atr_pct, settings.TRAILING_MIN_PCT), settings.TRAILING_MAX_PCT)
    return trail, settings.TRAILING_ARM_ATR_MULT * atr_pct


def evaluate_exit(
    avg_price: float, peak_price: Optional[float], current_price: float, atr_pct: Optional[float] = None
) -> Optional[ExitDecision]:
    if avg_price <= 0 or current_price <= 0:
        return None

    if (current_price - avg_price) / avg_price <= -settings.STOP_LOSS_PCT:
        return ExitDecision(reason="STOP_LOSS")

    # 트레일링은 고점이 평단 대비 "충분한 이익"(무장 기준)에 도달한 적이 있을 때만 켠다.
    # 평단을 살짝만 넘은 고점에서 폭만큼 빠지면 익절이 아니라 손실 매도가 되므로(라벨과 실제가
    # 어긋남), 그 구간은 손절(STOP_LOSS)이 담당한다. 폭/무장 기준은 ATR로 종목별 산출한다.
    trail_pct, arm_pct = trailing_params(atr_pct)
    peak = max(peak_price or avg_price, avg_price)
    if (peak - avg_price) / avg_price >= arm_pct and (current_price - peak) / peak <= -trail_pct:
        return ExitDecision(reason="TRAILING_TAKE_PROFIT")

    return None
