# core/llm/base.py
"""LLM 및 System-1 의사결정 제공자 인터페이스.

Gemini / Open-Jev 등 어떤 provider를 붙여도 signal_engine / rebalancer / intel 데몬은
이 추상 인터페이스의 반환 계약만 알면 된다 (Dependency Inversion / Provider-Agnostic).

중요: 파싱 실패·네트워크 오류·예산 초과 등 모든 예외 상황에서는 절대 강한 매수/매도로
오해될 수 있는 값을 반환하지 말고 안전한 HOLD/중립(또는 무변경 폴백)으로 귀결되어야 한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ModelTier(str, Enum):
    """태스크 성격에 따른 3티어 모델 구분."""

    DECISION = "decision"  # Tier 1: 로컬 Open-Jev (System 1 선택/분류/점수, $0)
    FAST = "fast"          # Tier 2: 초경량 Gemini (요약/번역/리포트 배치)
    SMART = "smart"        # Tier 3: 고지능 Gemini (포트폴리오 리밸런싱 등)


@dataclass(frozen=True)
class DecisionResult:
    """System-1 모델의 타입-안전 선택지 결정 결과."""

    label: str
    confidence: float
    probabilities: Dict[str, float]
    latency_ms: float
    model: str


class DecisionProvider(ABC):
    """텍스트 생성 없이 선택지/점수 확률만 반환하는 System-1 의사결정 인터페이스."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """모델 식별자 (예: 'laya', 'clef', 'von')."""

    @abstractmethod
    async def classify_news_veto(
        self, symbol: str, title: str, summary: str
    ) -> DecisionResult:
        """뉴스의 매매 영향 방향('BUY' | 'SELL' | 'HOLD')과 보정 확률을 반환한다."""

    @abstractmethod
    async def score_news_surprise(
        self, symbol: str, title: str, summary: str
    ) -> Tuple[int, str, float]:
        """미장 뉴스의 1차 Surprise 점수(0~100), 영향('호재'|'악재'|'중립'), 신뢰도(0~1)를 반환한다."""


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
        """포트폴리오 목표비중 초안을 제안한다 (사용자 승인 전까지는 활성화되지 않음)."""

    @abstractmethod
    async def propose_portfolio_changes(
        self,
        *,
        current_positions: List[Dict[str, Any]],
        current_signals: Dict[str, Dict[str, Any]],
        candidate_pool: List[Dict[str, Any]],
        macro_context: Optional[str],
        insight_digest: Optional[Dict[str, Any]] = None,
        max_symbols: int,
        min_weight: float,
        max_weight: float,
        max_symbols_added: int = 2,
        max_symbols_removed: int = 2,
        max_weight_delta_pct: float = 0.10,
        max_turnover_pct: float = 0.20,
        total_equity: Optional[float] = None,
    ) -> Dict[str, Any]:
        """현재 포트폴리오 맥락과 후보종목 숏리스트를 근거로 리밸런싱을 제안한다.

        실패 시: adds=[], removes=[], weights=<current_positions 유지>, degraded=True.
        """

    @abstractmethod
    async def analyze_research_reports(
        self, reports: List[Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        """증권사 리포트 배치를 분석한다."""

    @abstractmethod
    async def summarize_insight_batch(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """발굴 갱신 1회의 종합 데이터를 바탕으로 일일 AI 다이제스트를 생성한다."""
