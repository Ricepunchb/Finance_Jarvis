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


def evaluate_exit(avg_price: float, peak_price: Optional[float], current_price: float) -> Optional[ExitDecision]:
    if avg_price <= 0 or current_price <= 0:
        return None

    if (current_price - avg_price) / avg_price <= -settings.STOP_LOSS_PCT:
        return ExitDecision(reason="STOP_LOSS")

    # peak가 avg_price보다 높았던 적(한 번이라도 이익 구간)이 있을 때만 트레일링을 "무장"한다.
    # 안 그러면 진입 직후 단순 하락(아직 이익을 본 적 없음)도 트레일링 익절로 오발동해
    # 손절(STOP_LOSS)과 구분이 안 된다 — 그 케이스는 위 손절 조건이 담당한다.
    peak = max(peak_price or avg_price, avg_price)
    if peak > avg_price and (current_price - peak) / peak <= -settings.TRAILING_TAKE_PROFIT_PCT:
        return ExitDecision(reason="TRAILING_TAKE_PROFIT")

    return None
