# core/ai_rebalance_guard.py
"""AI 포트폴리오 에이전트가 낸 제안의 안전 범위를 검증한다. core/rebalancer.py와 같은
스타일 — I/O 없음, 주문/제안을 만들지 않고 판단만 한다.

위반 시 숫자를 잘라서 맞추지 않고(clamp) 통째로 반려한다 — 잘라서 맞추면 비중 합이
깨지고 LLM의 rationale과 실제 숫자가 어긋나게 되므로, "거부 후 무변경 유지"가
"숫자를 몰래 축소"보다 안전하다.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def compute_effective_max_symbols(
    base_max_symbols: int,
    total_equity: Optional[float],
    min_position_value_krw: float,
    min_symbols_floor: int = 4,
) -> int:
    """계좌 총자산(total_equity) 규모에 맞춰 최대 보유 종목 수를 축소한다.
    총자산 정보가 없으면(None 또는 <= 0) 기본 상한을 그대로 반환한다."""
    if total_equity is None or total_equity <= 0 or min_position_value_krw <= 0:
        return base_max_symbols
    budget_cap = max(min_symbols_floor, int(total_equity // min_position_value_krw))
    return min(base_max_symbols, budget_cap)


def compute_effective_add_weight_cap(
    max_portfolio_symbols: int,
    max_weight_delta_pct: float,
    max_symbol_weight_pct: float,
) -> float:
    """신규 편입 종목(adds)에 허용할 1회 초기 비중 상한.
    소액 계좌로 종목 수가 줄어든 경우(예: 4종목) 1/N 균등 비중(25%)까지는 초기 편입을 허용해야
    기존 종목 교체 시 1주 매수 가능한 비중으로 들어갈 수 있다."""
    if max_portfolio_symbols <= 0:
        return min(max_symbol_weight_pct, max_weight_delta_pct)
    equal_slot = round(1.0 / max_portfolio_symbols, 4)
    return min(max_symbol_weight_pct, max(max_weight_delta_pct, equal_slot))


def compute_effective_turnover_cap(
    base_turnover_pct: float,
    max_portfolio_symbols: int,
) -> float:
    """소액 계좌(예: 4종목 × 25%)에서도 최소 1종목 1:1 교체(25% 제외 + 25% 편입 = 회전율 25%)가
    회전율 가드에 원천 차단되지 않도록 1/N 수준(최대 0.30)까지 회전율 상한을 보정한다."""
    if max_portfolio_symbols <= 0:
        return base_turnover_pct
    swap_turnover = min(0.30, round(1.0 / max_portfolio_symbols, 4))
    return max(base_turnover_pct, swap_turnover)


def compute_turnover(old_weights: Dict[str, float], new_weights: Dict[str, float]) -> float:
    symbols = set(old_weights) | set(new_weights)
    return sum(abs(new_weights.get(s, 0.0) - old_weights.get(s, 0.0)) for s in symbols) / 2


def validate_proposal(
    proposed_weights: Dict[str, float],
    active_weights: Dict[str, float],
    adds: List[str],
    removes: List[str],
    *,
    max_turnover_pct: float,
    max_weight_delta_pct: float,
    max_symbols_added: int,
    max_symbols_removed: int,
    min_symbol_weight_pct: float,
    max_symbol_weight_pct: float,
    max_portfolio_symbols: int,
    total_equity: Optional[float] = None,
    symbol_prices: Optional[Dict[str, float]] = None,
    held_quantities: Optional[Dict[str, float]] = None,
    max_add_weight_pct: Optional[float] = None,
) -> ValidationResult:
    if not proposed_weights:
        return ValidationResult(False, "제안된 비중이 비어 있음")

    # LLM이 종목 청산/제외 의도로 0.0 대신 0.001(0.1%) 등의 극소 비중을 출력한 경우, 0.0(제외)으로 스냅
    for s, w in list(proposed_weights.items()):
        if 0 < w < 0.005:
            proposed_weights[s] = 0.0

    # 현금 보유(미배분)는 정상 상태다 (예: 라이브 포트폴리오도 3종목 x 15% = 45%만 배분,
    # 나머지 55%는 의도적 현금 — REBALANCE_BAND_PCT/MAX_POSITION_PCT 기반 보수적 운용).
    # 합이 1.0을 넘는 것만 반려한다(그 종목들의 실제 비중 합이 100%를 초과할 수는 없으므로).
    total = sum(proposed_weights.values())
    if total > 1.02:
        return ValidationResult(False, f"비중 합이 1.0을 초과함 (합={total:.3f})")

    # 비중 0은 "제외(보유하지 않음)"의 정상적인 표현이다 - 종목 수/하한 검사에서는
    # 실제로 보유하려는 종목(비중>0)만 센다.
    held_weights = {s: w for s, w in proposed_weights.items() if w > 0}
    active_held_count = sum(1 for w in active_weights.values() if w > 0)

    if active_held_count <= max_portfolio_symbols:
        if len(held_weights) > max_portfolio_symbols:
            return ValidationResult(
                False, f"종목 수 상한 초과 ({len(held_weights)} > {max_portfolio_symbols})"
            )
    else:
        # 이미 자금 규모 상한을 초과한 포트폴리오라도 최종 종목 수를 상한 이하로 맞추는 교체 편입은 허용한다.
        if adds and len(held_weights) > max_portfolio_symbols:
            return ValidationResult(
                False,
                f"현재 종목 수({active_held_count})가 자금 규모 상한({max_portfolio_symbols})을 "
                f"초과하여 신규 편입 불가 (먼저 {max_portfolio_symbols}개 이하로 축소 필요)",
            )
        if not adds and len(held_weights) >= active_held_count and max_symbols_removed > 0:
            return ValidationResult(
                False,
                f"자금 규모 상한({max_portfolio_symbols}개) 초과 상태이므로 종목 수를 "
                f"현재({active_held_count}개)보다 줄여야 함 (제안={len(held_weights)}개)",
            )

    for symbol, weight in held_weights.items():
        if weight < min_symbol_weight_pct or weight > max_symbol_weight_pct:
            return ValidationResult(
                False,
                f"{symbol} 비중 {weight:.1%}이 허용 범위 "
                f"[{min_symbol_weight_pct:.1%}, {max_symbol_weight_pct:.1%}] 밖",
            )

    if total_equity is not None and total_equity > 0 and symbol_prices:
        add_set = set(adds)
        for symbol, weight in held_weights.items():
            price = symbol_prices.get(symbol)
            if price is None or price <= 0:
                continue
            target_alloc = weight * total_equity
            is_new_add = symbol in add_set
            is_unheld_increase = (
                (held_quantities is None or held_quantities.get(symbol, 0.0) <= 0)
                and weight > active_weights.get(symbol, 0.0)
            )
            if (is_new_add or is_unheld_increase) and target_alloc < price:
                return ValidationResult(
                    False,
                    f"{symbol} 1주 가격({price:,.0f}원)이 목표 비중 배분액"
                    f"({target_alloc:,.0f}원, {weight:.1%})을 초과하여 1주 매수 불가",
                )

    turnover = compute_turnover(active_weights, proposed_weights)
    if turnover > max_turnover_pct + 1e-9:
        return ValidationResult(False, f"회전율 상한 초과 (turnover={turnover:.1%} > {max_turnover_pct:.1%})")

    # 제외(removes) 대상은 의도적 전량 축소(목표비중 0)이므로 종목당 변화폭 상한을 적용하지
    # 않는다 - 보유비중이 크던 작던 "뺀다"는 결정 자체는 max_symbols_removed로 별도 제한된다.
    # (반면 그냥 재조정 중인 보유종목이 갑자기 크게 변하는 것은 여기서 계속 막는다.)
    remove_set = set(removes)
    add_set = set(adds)
    add_delta_cap = (
        max(max_weight_delta_pct, max_add_weight_pct)
        if max_add_weight_pct is not None
        else max_weight_delta_pct
    )
    for symbol in set(active_weights) | set(proposed_weights):
        if symbol in remove_set:
            continue
        delta = abs(proposed_weights.get(symbol, 0.0) - active_weights.get(symbol, 0.0))
        allowed_delta = add_delta_cap if symbol in add_set else max_weight_delta_pct
        if delta > allowed_delta + 1e-9:
            return ValidationResult(
                False, f"{symbol} 비중 변화폭 {delta:.1%}이 상한 {allowed_delta:.1%} 초과"
            )

    if len(adds) > max_symbols_added:
        return ValidationResult(False, f"신규 편입 종목 수 상한 초과 ({len(adds)} > {max_symbols_added})")
    if len(removes) > max_symbols_removed:
        return ValidationResult(False, f"제외 종목 수 상한 초과 ({len(removes)} > {max_symbols_removed})")

    return ValidationResult(True)
