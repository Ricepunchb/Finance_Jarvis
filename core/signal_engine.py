# core/signal_engine.py
"""종목 1개·사이클 1회에 대해 정확히 하나의 액션(또는 NO_OP)을 만드는 단일 결정 함수.

목표비중 밴드(rebalancer)와 기술/뉴스 시그널을 여기서 하나의 if/else로 합친다.
밴드 밖이면 방향은 항상 밴드가 정하고, 시그널은 타이밍/크기만 조정한다. 밴드 안일 때만
시그널이 방향까지 정할 수 있다 — 두 소스가 동시에 서로 다른 주문을 만들 수 있는
코드 경로가 구조적으로 존재하지 않는다.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.rebalancer import compute_drift
from core.risk import RiskManager

# 밴드 안에서 시그널만으로 전술매매를 시작하려면 넘어야 하는 최소 강도.
TACTICAL_SIGNAL_THRESHOLD = 0.5
# 밴드 교정 방향과 정반대로 강하게 반대하는 시그널로 간주하는 강도.
CONFLICT_SIGNAL_THRESHOLD = 0.7
# 밴드 안 전술매매 1회의 최대 크기 (총자산 대비).
TACTICAL_TRADE_MAX_EQUITY_FRACTION = 0.02

Signal = Dict[str, Any]  # {"direction": "BUY"|"SELL"|"HOLD", "strength": float}


@dataclass
class Action:
    side: str  # "buy" | "sell"
    qty: float
    order_type: str  # "limit" | "market"
    price: Optional[float]
    reason: str


def _score(sig: Signal) -> float:
    m = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}
    return m.get(sig.get("direction", "HOLD"), 0.0) * sig.get("strength", 0.0)


def combine_signals(tech: Signal, sentiment: Optional[Signal]) -> Signal:
    tech_score = _score(tech)
    combined_score = tech_score if sentiment is None else 0.5 * tech_score + 0.5 * _score(sentiment)
    direction = "BUY" if combined_score > 0.15 else "SELL" if combined_score < -0.15 else "HOLD"
    return {"direction": direction, "strength": min(1.0, abs(combined_score))}


async def decide(
    *,
    current_weight: float,
    target_weight: float,
    band: float,
    tech_signal: Signal,
    sentiment_signal: Optional[Signal],
    price: float,
    current_position_qty: float,
    current_position_value: float,
    total_equity: float,
    risk: RiskManager,
) -> Optional[Action]:
    if price <= 0 or total_equity <= 0:
        return None

    drift = compute_drift(current_weight, target_weight, band)
    combined = combine_signals(tech_signal, sentiment_signal)

    if drift.required_direction is not None:
        # 밴드 밖: 방향은 항상 밴드가 결정한다. 시그널은 크기(타이밍)만 조정할 수 있다.
        side = "sell" if drift.required_direction == "SELL" else "buy"
        edge_value = abs(current_weight - drift.edge_weight()) * total_equity
        qty = edge_value / price

        opposes = combined["direction"] not in ("HOLD", drift.required_direction)
        if opposes and combined["strength"] >= CONFLICT_SIGNAL_THRESHOLD:
            qty *= 0.5  # 정반대 시그널이 강하면 절반만 교정하고 다음 사이클에 재평가
            reason = "band_correction_partial_due_to_signal_conflict"
        else:
            reason = "band_correction"
    else:
        # 밴드 안: 시그널이 방향까지 정할 수 있는 유일한 경우 — 단, 반드시 기술 시그널이
        # 먼저 방향을 제시해야 한다(Plan A: 코드/지표가 후보를 내고 감성은 사이징만).
        # 기술 시그널이 HOLD면 감성 시그널이 아무리 강해도 단독으로 매매를 개시하지 않는다.
        if tech_signal["direction"] == "HOLD":
            return None
        if combined["direction"] != tech_signal["direction"]:
            return None  # 감성이 기술 방향을 뒤집은 경우도 개시하지 않음
        if combined["strength"] < TACTICAL_SIGNAL_THRESHOLD:
            return None
        side = tech_signal["direction"].lower()
        tactical_value = combined["strength"] * TACTICAL_TRADE_MAX_EQUITY_FRACTION * total_equity
        # 반대편 밴드 경계를 뚫지 않는 크기로만 제한한다.
        band_edge = target_weight + band if side == "buy" else target_weight - band
        room_weight = (band_edge - current_weight) if side == "buy" else (current_weight - band_edge)
        room_value = max(0.0, room_weight * total_equity)
        tactical_value = min(tactical_value, room_value)
        if tactical_value <= 0:
            return None
        qty = tactical_value / price
        reason = "signal_timing"

    # 최종 리스크 클램프 (베토가 아니라 한도 축소)
    qty = risk.clamp_qty_to_notional(qty, price)
    if side == "buy":
        qty = risk.clamp_buy_qty_to_position_cap(qty, price, current_position_value, total_equity)
    else:
        qty = min(qty, current_position_qty)  # 보유 수량 이상 매도 불가

    qty = float(int(qty))  # 국내주식은 정수 단위 주문
    if qty <= 0:
        return None

    return Action(side=side, qty=qty, order_type="limit", price=price, reason=reason)
