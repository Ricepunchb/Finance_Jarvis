# core/signal_engine.py
"""종목 1개·사이클 1회에 대해 정확히 하나의 액션(또는 NO_OP)을 만드는 단일 결정 함수.

스윙 우선(swing-primary) 구조: 목표비중 밴드는 더 이상 매매 "방향"을 정하지 않는다.
밴드+버퍼를 넘어선 과대비중만 시그널과 무관하게 강제로 축소하는 상한(ceiling) 역할로
축소됐고, 그 외의 모든 매수/매도 방향과 타이밍은 기술+분봉(30분봉)+뉴스감성+밸류에이션
시그널을 결합한 스윙 시그널이 정한다. 저비중은 리스크가 아니므로 "강제 매수"는 만들지
않는다 — 매수 여부/시점은 전적으로 스윙 시그널의 몫이다.

손절/트레일링익절(core/exit_guard.py)은 이 함수와 별개의, 의도적으로 우회하는 경로다
(engine.py에서 decide() 호출 전에 먼저 평가된다) — 이 함수는 "평시" 매매만 다룬다.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.config import settings
from core.rebalancer import compute_drift
from core.risk import RiskManager

# 밴드 안에서 시그널만으로 스윙매매를 시작하려면 넘어야 하는 최소 강도.
SWING_SIGNAL_THRESHOLD = 0.5
# 밴드+버퍼를 넘으면(과대비중) 시그널과 무관하게 강제 축소하는 상한의 여유폭.
BAND_CEILING_BUFFER_PCT = 0.05
# 스윙매매 1회의 최대 크기 (총자산 대비, 시그널 강도에 비례해 스케일).
SWING_TRADE_MAX_EQUITY_FRACTION = 0.15

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


def combine_signals(
    tech: Signal,
    intraday: Optional[Signal],
    sentiment: Optional[Signal],
    valuation: Optional[Signal] = None,
) -> Signal:
    """존재하는 시그널끼리만 가중치를 재정규화한다 — 분봉 데이터가 아직 부족한 종목이나
    밸류에이션이 안 붙는 해외종목도 나머지 시그널만으로 그대로 동작한다."""
    parts = [(tech, 0.30)]
    if intraday is not None:
        parts.append((intraday, 0.50))  # 스윙의 주 동력
    if sentiment is not None:
        parts.append((sentiment, 0.20))
    if valuation is not None:
        parts.append((valuation, 0.20))

    total_weight = sum(w for _, w in parts)
    combined_score = sum(_score(sig) * w for sig, w in parts) / total_weight
    direction = "BUY" if combined_score > 0.15 else "SELL" if combined_score < -0.15 else "HOLD"
    return {"direction": direction, "strength": min(1.0, abs(combined_score))}


async def _onboarding_action(
    *, symbol: Optional[str], combined: Signal, drift, target_weight: float, price: float,
    current_position_value: float, total_equity: float, risk: RiskManager,
) -> Optional[Action]:
    """분할 온보딩 매수. 스윙 시그널 문턱(0.5)은 "언제 비중을 키우느냐"를 정하는 용도라, 신규
    편입/저비중 종목이 목표비중까지 가는 경로가 없었다. 퀀트의 통상 방식대로 목표비중과의 갭을
    (목표금액 / ONBOARDING_DAYS)의 일일 예산만큼 여러 사이클에 나눠 메운다(TWAP 성격).
    시그널은 진입 여부가 아니라 "명백한 하락 신호일 때만 보류"하는 최소 필터로만 쓴다."""
    if not settings.ONBOARDING_ENABLED or symbol is None or target_weight <= 0:
        return None
    if drift.required_direction != "BUY":  # 밴드 안이면 이미 충분히 채워진 것
        return None
    if combined["direction"] == "SELL":
        return None

    gap_value = (target_weight - drift.current_weight) * total_equity
    daily_budget = target_weight * total_equity / max(1, settings.ONBOARDING_DAYS)
    remaining_today = daily_budget - await risk.onboarding_notional_today(symbol)
    trade_value = min(gap_value, remaining_today)
    if trade_value <= 0:
        return None

    qty = risk.clamp_qty_to_notional(trade_value / price, price)
    qty = risk.clamp_buy_qty_to_position_cap(qty, price, current_position_value, total_equity)
    qty = float(int(qty))
    # 1주가 1회 주문 한도보다 비싼 종목도 편입은 가능해야 한다 — 한도의 2배까지는 1주를 허용.
    if qty <= 0 and price <= 2 * settings.MAX_ORDER_NOTIONAL_KRW and trade_value >= price:
        if risk.clamp_buy_qty_to_position_cap(1.0, price, current_position_value, total_equity) >= 1.0:
            qty = 1.0
    if qty <= 0:
        return None
    return Action(side="buy", qty=qty, order_type="limit", price=price, reason="onboarding_tranche")


async def decide(
    *,
    symbol: Optional[str] = None,
    current_weight: float,
    target_weight: float,
    band: float,
    tech_signal: Signal,
    intraday_signal: Optional[Signal],
    sentiment_signal: Optional[Signal],
    valuation_signal: Optional[Signal] = None,
    price: float,
    current_position_qty: float,
    current_position_value: float,
    total_equity: float,
    risk: RiskManager,
) -> Optional[Action]:
    if price <= 0 or total_equity <= 0:
        return None

    if target_weight <= 0:
        # "제외 종목" — 관심(모니터링) 대상이 아니라 포트폴리오에서 뺀 종목이다. 신규 매수는
        # 시그널이 아무리 강해도 하지 않고, 잔여 보유분은 밴드 경계가 아니라 전량 청산한다
        # (밴드 상한 로직은 목표 0이어도 band+buffer만큼을 남겨두므로 청산이 끝나지 않는다).
        # 1회 주문 한도(clamp)로 여러 사이클에 나눠 팔릴 수 있다. 보유가 없으면 할 일 없음.
        if current_position_qty <= 0:
            return None
        side = "sell"
        # 주문 한도보다 비싼 종목(1주 > 한도)도 청산은 되어야 하므로 최소 1주는 허용한다.
        qty = risk.clamp_qty_to_notional(current_position_qty, price)
        qty = float(max(1, int(min(qty, current_position_qty))))
        return Action(side=side, qty=qty, order_type="limit", price=price, reason="excluded_liquidation")

    combined = combine_signals(tech_signal, intraday_signal, sentiment_signal, valuation_signal)
    drift = compute_drift(current_weight, target_weight, band)
    ceiling_weight = target_weight + band + BAND_CEILING_BUFFER_PCT

    if drift.required_direction == "SELL" and current_weight > ceiling_weight:
        # 밴드+버퍼를 넘어선 과대비중 — 시그널이 아무리 강한 BUY라도 협상 대상이 아니다.
        # 목표비중이 아니라 밴드 경계(edge_weight())까지만 축소한다(경계 안쪽은 시그널의 몫).
        side = "sell"
        qty = (current_weight - drift.edge_weight()) * total_equity / price
        reason = "band_ceiling_forced_trim"
    else:
        if combined["direction"] == "HOLD" or combined["strength"] < SWING_SIGNAL_THRESHOLD:
            return await _onboarding_action(
                symbol=symbol, combined=combined, drift=drift, target_weight=target_weight, price=price,
                current_position_value=current_position_value, total_equity=total_equity, risk=risk,
            )
        side = combined["direction"].lower()
        trade_value = combined["strength"] * SWING_TRADE_MAX_EQUITY_FRACTION * total_equity
        if side == "buy":
            room_value = max(0.0, ceiling_weight * total_equity - current_position_value)
            trade_value = min(trade_value, room_value)
        else:
            trade_value = min(trade_value, current_position_value)
        if trade_value <= 0:
            return None
        qty = trade_value / price
        reason = "swing_signal"

    # 최종 리스크 클램프 (베토가 아니라 한도 축소) — 기존과 동일
    qty = risk.clamp_qty_to_notional(qty, price)
    if side == "buy":
        qty = risk.clamp_buy_qty_to_position_cap(qty, price, current_position_value, total_equity)
    else:
        qty = min(qty, current_position_qty)  # 보유 수량 이상 매도 불가

    qty = float(int(qty))  # 국내주식은 정수 단위 주문
    if qty <= 0:
        return None

    return Action(side=side, qty=qty, order_type="limit", price=price, reason=reason)
