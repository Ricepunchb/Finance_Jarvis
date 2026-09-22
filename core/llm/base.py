# core/llm/base.py
"""LLM 제공자 인터페이스. Gemini/OpenAI/Claude 등 어떤 provider를 붙여도
signal_engine/rebalancer는 이 두 메서드의 반환 형태만 알면 된다 (provider-agnostic).

중요: 이 인터페이스를 구현하는 provider는 반드시 "런타임에 실제 LLM API를 호출"해야
한다 — 여기서 하드코딩된 판단을 대신 내리면 안 된다(사용자의 명시적 요구사항).
파싱 실패/네트워크 오류 등 모든 예외 상황에서는 절대 강한 매수/매도로 오해될 수 있는
값을 반환하지 말고 안전한 HOLD/중립으로 귀결되어야 한다.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class LLMProvider(ABC):
    @abstractmethod
    async def analyze_news(self, symbol: str, title: str, summary: str) -> Dict[str, Any]:
        """뉴스 한 건의 감성을 분석한다.

        Returns:
            {"direction": "BUY"|"SELL"|"HOLD", "strength": float(0~1), "reasoning": str}
            실패 시 항상 {"direction": "HOLD", "strength": 0.0, "reasoning": "<사유>"}.
        """

    @abstractmethod
    async def propose_weights(self, symbols: List[str]) -> Dict[str, Any]:
        """포트폴리오 목표비중 초안을 제안한다 (사용자 승인 전까지는 활성화되지 않음).

        Returns:
            {"weights": {symbol: weight, ...}, "rationale": str}
            weights의 합은 1.0에 근접해야 하지만, 승인 단계에서 사람이 다시 검토한다는
            전제로 여기서는 엄격히 강제하지 않는다.
        """

    @abstractmethod
    async def propose_portfolio_changes(
        self,
        *,
        current_positions: List[Dict[str, Any]],
        current_signals: Dict[str, Dict[str, Any]],
        candidate_pool: List[Dict[str, Any]],
        macro_context: Optional[str],
        max_symbols: int,
    ) -> Dict[str, Any]:
        """현재 포트폴리오 맥락(비중/손익/시그널)과 후보종목 숏리스트를 근거로 리밸런싱을
        제안한다 (사용자 승인 전까지는 활성화되지 않음). candidate_pool이 비어 있으면
        신규 종목 발굴 없이 기존 보유종목의 비중만 재검토한다.

        Args:
            current_positions: [{"symbol", "weight", "qty", "avg_price", ...}] 현재 활성 포트폴리오.
            current_signals: symbol -> {"tech":..., "intraday":..., "sentiment":..., "valuation":...}.
            candidate_pool: [{"symbol", "name", ...}] 신규 편입 후보 숏리스트. 이 목록에 없는
                종목은 절대 adds에 포함시키면 안 된다는 것을 프롬프트에 명시해야 한다.
            macro_context: 선택적 자유 텍스트(거시 이벤트 등). 없으면 None.
            max_symbols: 최종 포트폴리오가 넘지 말아야 할 종목 수 상한.

        Returns:
            {
              "adds": [{"symbol", "name", "rationale"}],
              "removes": [{"symbol", "rationale"}],
              "weights": {symbol: weight, ...},   # add/remove 반영 후 전체 목표비중, 합은 1.0 이하
                                                   # (전액 투자를 강제하지 않음 - 현금 보유 가능)
              "rationale": str,
              "degraded": bool,             # True면 LLM이 정상 판단하지 못했다는 명시적 신호
              "error_kind": Optional[str],  # 'llm_error' | 'schema_violation' | 'invalid_weights' | None
            }
            실패 시: adds=[], removes=[], weights=<current_positions의 비중을 그대로 유지>,
            degraded=True. "균등비중 폴백"이 아니라 "무변경 폴백"이어야 한다 — 살아있는
            포트폴리오를 LLM 오류로 흩어버리는 것은 무변경보다 더 위험하다.
        """
