# core/llm/base.py
"""LLM 제공자 인터페이스. Gemini/OpenAI/Claude 등 어떤 provider를 붙여도
signal_engine/rebalancer는 이 두 메서드의 반환 형태만 알면 된다 (provider-agnostic).

중요: 이 인터페이스를 구현하는 provider는 반드시 "런타임에 실제 LLM API를 호출"해야
한다 — 여기서 하드코딩된 판단을 대신 내리면 안 된다(사용자의 명시적 요구사항).
파싱 실패/네트워크 오류 등 모든 예외 상황에서는 절대 강한 매수/매도로 오해될 수 있는
값을 반환하지 말고 안전한 HOLD/중립으로 귀결되어야 한다.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List


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
