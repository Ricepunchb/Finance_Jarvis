# core/ai_rebalance_guard.py
"""AI 포트폴리오 에이전트가 낸 제안의 안전 범위를 검증한다. core/rebalancer.py와 같은
스타일 — I/O 없음, 주문/제안을 만들지 않고 판단만 한다.

위반 시 숫자를 잘라서 맞추지 않고(clamp) 통째로 반려한다 — 잘라서 맞추면 비중 합이
깨지고 LLM의 rationale과 실제 숫자가 어긋나게 되므로, "거부 후 무변경 유지"가
"숫자를 몰래 축소"보다 안전하다.
"""
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


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
) -> ValidationResult:
    if not proposed_weights:
        return ValidationResult(False, "제안된 비중이 비어 있음")

    # 현금 보유(미배분)는 정상 상태다 (예: 라이브 포트폴리오도 3종목 x 15% = 45%만 배분,
    # 나머지 55%는 의도적 현금 — REBALANCE_BAND_PCT/MAX_POSITION_PCT 기반 보수적 운용).
    # 합이 1.0을 넘는 것만 반려한다(그 종목들의 실제 비중 합이 100%를 초과할 수는 없으므로).
    total = sum(proposed_weights.values())
    if total > 1.02:
        return ValidationResult(False, f"비중 합이 1.0을 초과함 (합={total:.3f})")

    # 비중 0은 "제외(보유하지 않음)"의 정상적인 표현이다 - 종목 수/하한 검사에서는
    # 실제로 보유하려는 종목(비중>0)만 센다.
    held_weights = {s: w for s, w in proposed_weights.items() if w > 0}

    if len(held_weights) > max_portfolio_symbols:
        return ValidationResult(
            False, f"종목 수 상한 초과 ({len(held_weights)} > {max_portfolio_symbols})"
        )

    for symbol, weight in held_weights.items():
        if weight < min_symbol_weight_pct or weight > max_symbol_weight_pct:
            return ValidationResult(
                False,
                f"{symbol} 비중 {weight:.1%}이 허용 범위 "
                f"[{min_symbol_weight_pct:.1%}, {max_symbol_weight_pct:.1%}] 밖",
            )

    turnover = compute_turnover(active_weights, proposed_weights)
    if turnover > max_turnover_pct:
        return ValidationResult(False, f"회전율 상한 초과 (turnover={turnover:.1%} > {max_turnover_pct:.1%})")

    # 제외(removes) 대상은 의도적 전량 축소(목표비중 0)이므로 종목당 변화폭 상한을 적용하지
    # 않는다 - 보유비중이 크던 작던 "뺀다"는 결정 자체는 max_symbols_removed로 별도 제한된다.
    # (반면 그냥 재조정 중인 보유종목이 갑자기 크게 변하는 것은 여기서 계속 막는다.)
    remove_set = set(removes)
    for symbol in set(active_weights) | set(proposed_weights):
        if symbol in remove_set:
            continue
        delta = abs(proposed_weights.get(symbol, 0.0) - active_weights.get(symbol, 0.0))
        if delta > max_weight_delta_pct:
            return ValidationResult(
                False, f"{symbol} 비중 변화폭 {delta:.1%}이 상한 {max_weight_delta_pct:.1%} 초과"
            )

    if len(adds) > max_symbols_added:
        return ValidationResult(False, f"신규 편입 종목 수 상한 초과 ({len(adds)} > {max_symbols_added})")
    if len(removes) > max_symbols_removed:
        return ValidationResult(False, f"제외 종목 수 상한 초과 ({len(removes)} > {max_symbols_removed})")

    return ValidationResult(True)
