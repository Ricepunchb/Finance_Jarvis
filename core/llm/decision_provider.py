# core/llm/decision_provider.py
"""Tier 1 로컬 Open-Jev (System 1) 의사결정 어댑터 및 섀도 라우터.

`Laya` / `Clef` / `Von` 등 오픈소스 System-1 모델을 통일된 `DecisionProvider`
인터페이스로 감싸고, `ShadowDecisionRouter`를 통해 실전 투입 전 Gemini Fast와
병행 로깅(shadow)하거나 실전 전환(active)할 수 있도록 한다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from core.config import settings
from core.intel.usage_tracker import UsageTracker
from core.llm.base import DecisionProvider, DecisionResult, LLMProvider, ModelTier

logger = logging.getLogger(__name__)

_NEWS_VETO_CRITERIA: Dict[str, str] = {
    "BUY": "Strong positive catalyst for the stock price (earnings beat, major contract, buyback, upgrade).",
    "SELL": "Severe negative bad news that risks a sharp stock drop (earnings miss, lawsuit, probe, export ban, recall, fraud, dilution).",
    "HOLD": "Routine, neutral, or opinion news with minimal immediate stock price impact.",
}

_SURPRISE_BAND_CRITERIA: Dict[str, str] = {
    "LOW": "Routine market chatter, opinion column, or already priced-in news (score ~25).",
    "MEDIUM": "Normal in-line earnings, minor target price update, or routine contract (score ~55).",
    "HIGH": "Significant unexpected surprise: big earnings beat/miss, guidance cut, major contract, or regulatory probe (score ~80).",
    "EXTREME": "Black-swan shock: export ban, fraud/delisting, massive M&A, or emergency rate shock (score ~92).",
}
_SURPRISE_BAND_SCORE: Dict[str, int] = {"LOW": 25, "MEDIUM": 55, "HIGH": 80, "EXTREME": 92}


class LayaLocalDecisionProvider(DecisionProvider):
    """A/B 벤치마크 승자 Laya 로컬 인프로세스 System-1 제공자 (비용 $0, ~18ms).

    한국어 포함 텍스트는 `convaiinnovations/laya(multilingual)`, 영문 텍스트는
    `convaiinnovations/laya(english)`로 자동 라우팅하여 KR/US 정확도를 극대화한다.
    (`laya` 패키지는 지연 임포트하므로 미설치 환경에서도 모듈 임포트가 깨지지 않는다.)
    """

    def __init__(self, *, device: Optional[str] = None, mode: str = "router") -> None:
        self._device = device
        self._mode = mode  # "router" (en+multilingual) | "multilingual" | "en"
        self._agent_en: Any = None
        self._agent_ml: Any = None

    @property
    def model_name(self) -> str:
        return f"laya-{self._mode}"

    def _ensure_loaded(self, is_korean: bool) -> Any:
        import laya
        import torch

        dev = self._device or ("cuda" if torch.cuda.is_available() else "cpu")
        use_ml = self._mode == "multilingual" or (self._mode == "router" and is_korean)
        if use_ml:
            if self._agent_ml is None:
                self._agent_ml = laya.load("convaiinnovations/laya", subfolder="multilingual", device=dev)
            return self._agent_ml
        if self._agent_en is None:
            self._agent_en = laya.load("convaiinnovations/laya", subfolder=None, device=dev)
        return self._agent_en

    def _predict_sync(self, state: str, questions: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        has_kr = bool(re.search(r"[가-힣]", state))
        agent = self._ensure_loaded(has_kr)
        t0 = time.perf_counter()
        raw = agent.predict(state, questions)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        answers = raw.get("answers", {}) if isinstance(raw, dict) else {}
        return answers, elapsed_ms

    async def classify_news_veto(
        self, symbol: str, title: str, summary: str
    ) -> DecisionResult:
        state = f"Symbol: {symbol}\nTitle: {title}\nSummary: {summary}"
        questions = {
            "sentiment": {
                "type": "choice",
                "instructions": "Classify the immediate stock price impact of this financial news.",
                "criteria": _NEWS_VETO_CRITERIA,
            }
        }
        answers, elapsed_ms = await asyncio.to_thread(self._predict_sync, state, questions)
        sent = answers.get("sentiment") or {}
        label = str(sent.get("choice") or "HOLD").upper()
        if label not in ("BUY", "SELL", "HOLD"):
            label = "HOLD"
        probs = {str(k).upper(): float(v) for k, v in (sent.get("probabilities") or {}).items()}
        conf = float(sent.get("answer_confidence") or probs.get(label, 0.5))
        return DecisionResult(
            label=label,
            confidence=max(0.0, min(1.0, conf)),
            probabilities=probs or {label: conf},
            latency_ms=elapsed_ms,
            model=self.model_name,
        )

    async def score_news_surprise(
        self, symbol: str, title: str, summary: str
    ) -> Tuple[int, str, float]:
        state = f"Symbol: {symbol}\nTitle: {title}\nSummary: {summary}"
        questions = {
            "surprise_band": {
                "type": "choice",
                "instructions": "How unexpected or surprising is this news relative to market consensus?",
                "criteria": _SURPRISE_BAND_CRITERIA,
            },
            "sentiment": {
                "type": "choice",
                "instructions": "Classify the immediate stock price impact of this financial news.",
                "criteria": _NEWS_VETO_CRITERIA,
            },
        }
        answers, _ = await asyncio.to_thread(self._predict_sync, state, questions)
        band = str((answers.get("surprise_band") or {}).get("choice") or "LOW").upper()
        score = _SURPRISE_BAND_SCORE.get(band, 25)
        sent_label = str((answers.get("sentiment") or {}).get("choice") or "HOLD").upper()
        impact = "호재" if sent_label == "BUY" else "악재" if sent_label == "SELL" else "중립"
        conf = float((answers.get("surprise_band") or {}).get("answer_confidence") or 0.5)
        return score, impact, max(0.0, min(1.0, conf))


class HttpSystemOneDecisionProvider(DecisionProvider):
    """TypeSafe `/v1/systemone` 호환 로컬 Open-Jev 서버 클라이언트 (Von / Clef / Laya 서빙)."""

    def __init__(
        self,
        *,
        endpoint: str,
        model_id: str = "laya",
        timeout_sec: float = 3.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model_id = model_id
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)

    @property
    def model_name(self) -> str:
        return self._model_id

    async def _post_systemone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._endpoint}/v1/systemone"
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def classify_news_veto(
        self, symbol: str, title: str, summary: str
    ) -> DecisionResult:
        t0 = time.perf_counter()
        payload = {
            "model": self._model_id,
            "context": f"Symbol: {symbol}\nTitle: {title}\nSummary: {summary}",
            "questions": [
                {
                    "id": "sentiment",
                    "type": "choice",
                    "prompt": "What is the immediate stock price impact of this news?",
                    "choices": ["BUY", "SELL", "HOLD"],
                }
            ],
        }
        data = await self._post_systemone(payload)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ans = (data.get("answers") or [{}])[0]
        label = str(ans.get("choice") or "HOLD").upper()
        if label not in ("BUY", "SELL", "HOLD"):
            label = "HOLD"
        probs = ans.get("probabilities") or {label: float(ans.get("confidence", 0.5))}
        conf = float(ans.get("confidence", probs.get(label, 0.5)))
        return DecisionResult(
            label=label,
            confidence=max(0.0, min(1.0, conf)),
            probabilities={k.upper(): float(v) for k, v in probs.items()},
            latency_ms=elapsed_ms,
            model=self._model_id,
        )

    async def score_news_surprise(
        self, symbol: str, title: str, summary: str
    ) -> Tuple[int, str, float]:
        payload = {
            "model": self._model_id,
            "context": f"Symbol: {symbol}\nTitle: {title}\nSummary: {summary}",
            "questions": [
                {
                    "id": "surprise",
                    "type": "score",
                    "prompt": "How surprising or unexpected is this news relative to market consensus (0-100)?",
                    "min": 0,
                    "max": 100,
                },
                {
                    "id": "impact",
                    "type": "choice",
                    "prompt": "Impact direction on the stock",
                    "choices": ["호재", "악재", "중립"],
                },
            ],
        }
        data = await self._post_systemone(payload)
        answers = {a.get("id"): a for a in (data.get("answers") or [])}
        score_raw = answers.get("surprise", {}).get("score", 30)
        score = max(0, min(100, int(round(float(score_raw)))))
        impact = str(answers.get("impact", {}).get("choice") or "중립")
        if impact not in ("호재", "악재", "중립"):
            impact = "중립"
        conf = float(answers.get("surprise", {}).get("confidence", 0.5))
        return score, impact, max(0.0, min(1.0, conf))


class ShadowDecisionRouter(LLMProvider):
    """Tier 1(DecisionProvider)과 Tier 2/3(LLMProvider)를 조합하는 Decorator 라우터.

    - mode='shadow': Tier 2(Gemini Fast) 결과를 반환하되 Tier 1 결과를 비동기로 비교 로깅.
    - mode='active': `analyze_news`를 Tier 1 로컬 모델로 직접 처리($0), 실패 시 Tier 2 폴백.
    - 나머지 생성/리밸런싱 메서드는 그대로 하위 LLMProvider에 위임한다.
    """

    def __init__(
        self,
        *,
        llm_provider: LLMProvider,
        decision_provider: DecisionProvider,
        mode: str = "shadow",
        usage_tracker: Optional[UsageTracker] = None,
    ) -> None:
        self._llm = llm_provider
        self._decision = decision_provider
        self._mode = mode.lower()
        self._tracker = usage_tracker or UsageTracker()

    async def _generate(self, *args: Any, **kwargs: Any) -> Any:
        """하위 provider의 구조화 생성 호출을 그대로 위임한다."""
        return await self._llm._generate(*args, **kwargs)  # type: ignore[attr-defined]

    async def score_news_surprise(
        self, symbol: str, title: str, summary: str
    ) -> Optional[Tuple[int, str, float]]:
        """Tier 1 로컬 Open-Jev로 미장 뉴스 1차 Surprise 점수를 산출한다 (active 모드 전용, 비용 $0)."""
        if self._mode != "active":
            return None
        t0 = time.perf_counter()
        score, impact, conf = await self._decision.score_news_surprise(symbol, title, summary)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        await self._tracker.record(
            task_type="us_surprise_filter",
            tier=ModelTier.DECISION.value,
            provider="open_jev",
            model=self._decision.model_name,
            latency_ms=elapsed_ms,
            status="ok",
        )
        return score, impact, conf

    async def analyze_news(self, symbol: str, title: str, summary: str) -> Dict[str, Any]:
        if self._mode == "active":
            try:
                res = await self._decision.classify_news_veto(symbol, title, summary)
                await self._tracker.record(
                    task_type="news_sentiment",
                    tier=ModelTier.DECISION.value,
                    provider="open_jev",
                    model=res.model,
                    latency_ms=res.latency_ms,
                    status="ok",
                )
                return {
                    "direction": res.label,
                    "strength": res.confidence,
                    "reasoning": f"[Open-Jev:{res.model}] conf={res.confidence:.2f} ({res.latency_ms:.0f}ms)",
                }
            except Exception as e:
                logger.warning(f"Open-Jev '{self._decision.model_name}' 실패 — Gemini Fast 폴백: {e}")
                return await self._llm.analyze_news(symbol, title, summary)

        # shadow mode: Gemini 결과 반환 + Open-Jev 병행 기록
        llm_res = await self._llm.analyze_news(symbol, title, summary)
        try:
            dec_res = await self._decision.classify_news_veto(symbol, title, summary)
            agree = dec_res.label == llm_res.get("direction")
            logger.info(
                f"[Open-Jev Shadow] {symbol} agree={agree} "
                f"jev={dec_res.label}({dec_res.confidence:.2f},{dec_res.latency_ms:.0f}ms) "
                f"vs gemini={llm_res.get('direction')}({llm_res.get('strength', 0):.2f})"
            )
            await self._tracker.record(
                task_type="news_sentiment_shadow",
                tier=ModelTier.DECISION.value,
                provider="open_jev",
                model=dec_res.model,
                latency_ms=dec_res.latency_ms,
                status="shadow",
            )
        except Exception as e:
            logger.debug(f"[Open-Jev Shadow] 비교 호출 실패(무시): {e}")
        return llm_res

    async def propose_weights(self, symbols: List[str]) -> Dict[str, Any]:
        return await self._llm.propose_weights(symbols)

    async def propose_portfolio_changes(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._llm.propose_portfolio_changes(**kwargs)

    async def analyze_research_reports(
        self, reports: List[Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        return await self._llm.analyze_research_reports(reports)

    async def summarize_insight_batch(self, context: Dict[str, Any]) -> Dict[str, Any]:
        return await self._llm.summarize_insight_batch(context)
