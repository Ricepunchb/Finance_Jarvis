# core/rebalancer.py
"""목표비중 밴드 드리프트 계산만 담당한다. 브로커를 호출하지 않고 주문도 만들지 않는다.

이 모듈이 절대 주문을 만들지 않는다는 제약이 이중주문 방지의 핵심이다 —
"밴드 교정"과 "시그널 타이밍"이 서로 다른 코드에서 각자 주문을 낼 수 있는 구조라면
같은 사이클에 같은 종목에 두 번 주문이 나갈 위험이 생긴다. signal_engine.py가
이 모듈의 결과를 하나의 입력으로만 사용해 단일 결정을 내린다.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class DriftResult:
    current_weight: float
    target_weight: float
    band: float
    drift: float
    in_band: bool
    required_direction: Optional[str]  # "BUY" | "SELL" | None (밴드 안이면 None)

    def edge_weight(self) -> float:
        """밴드 밖일 때, 교정 목표로 삼을 밴드 경계 비중 (목표비중 자체가 아니라 경계까지만)."""
        if self.required_direction == "SELL":
            return self.target_weight + self.band
        if self.required_direction == "BUY":
            return self.target_weight - self.band
        return self.target_weight


def compute_drift(current_weight: float, target_weight: float, band: float) -> DriftResult:
    drift = current_weight - target_weight
    in_band = abs(drift) <= band
    required_direction = None if in_band else ("SELL" if drift > 0 else "BUY")
    return DriftResult(current_weight, target_weight, band, drift, in_band, required_direction)
