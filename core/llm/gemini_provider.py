# core/llm/gemini_provider.py
"""Google Gemini API를 실제로 호출하는 LLMProvider 구현체.

모든 호출은 response_schema로 구조화된 JSON을 강제해 파싱 실패 가능성을 줄이지만,
그래도 실패하면(네트워크 오류, 스키마 위반, 타임아웃 등) 절대 예외를 상위로 올리지
않고 안전한 HOLD로 귀결시킨다 — 자동매매 로직이 LLM 장애로 오작동하면 안 되기 때문.
"""
import logging
from typing import Any, Dict, List

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from core.config import settings
from core.llm.base import LLMProvider

logger = logging.getLogger(__name__)

SAFE_HOLD: Dict[str, Any] = {"direction": "HOLD", "strength": 0.0, "reasoning": ""}


class _NewsSentimentSchema(BaseModel):
    direction: str = Field(description="BUY, SELL, 또는 HOLD 중 하나")
    strength: float = Field(description="0.0(무의미)~1.0(매우 강함) 사이의 신호 강도")
    reasoning: str = Field(description="한국어 한 문장 근거")


class _WeightItem(BaseModel):
    symbol: str = Field(description="종목코드")
    weight: float = Field(description="목표비중 (0~1)")


class _WeightProposalSchema(BaseModel):
    # Gemini Developer API의 구조화 출력은 임의 키를 갖는 Dict(=JSON schema의
    # additionalProperties)를 지원하지 않는다 — 반드시 고정 필드를 가진 배열로 표현해야 한다.
    weights: List[_WeightItem] = Field(description="종목코드-비중 쌍의 목록, 비중의 합은 1.0에 근접")
    rationale: str = Field(description="한국어 2~3문장 근거")


class GeminiProvider(LLMProvider):
    def __init__(self):
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다.")
        self._client = genai.Client(api_key=settings.GEMINI_API_KEY)

    async def analyze_news(self, symbol: str, title: str, summary: str) -> Dict[str, Any]:
        prompt = (
            f"다음은 종목코드 {symbol}에 관한 뉴스다. 이 뉴스가 이 종목의 주가에 미칠 영향을 "
            f"분석하라. 확실하지 않으면 반드시 HOLD와 낮은 strength를 반환하라 "
            f"(과도한 확신을 가지지 말 것).\n\n제목: {title}\n요약: {summary}"
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_NewsSentimentSchema,
                    temperature=0.2,
                ),
            )
            parsed: _NewsSentimentSchema = response.parsed
            if parsed is None:
                raise ValueError("빈 응답")
            direction = parsed.direction.upper()
            if direction not in ("BUY", "SELL", "HOLD"):
                raise ValueError(f"알 수 없는 direction: {direction}")
            strength = max(0.0, min(1.0, parsed.strength))
            return {"direction": direction, "strength": strength, "reasoning": parsed.reasoning}
        except Exception:
            logger.exception(f"'{symbol}' 뉴스 감성분석 실패 - 안전하게 HOLD로 귀결")
            return dict(SAFE_HOLD)

    async def propose_weights(self, symbols: List[str]) -> Dict[str, Any]:
        prompt = (
            "다음 종목들로 구성된 포트폴리오의 목표 비중 초안을 제안하라. "
            "이 제안은 사람이 검토 후 승인해야만 실제로 적용된다는 점을 감안해 "
            "합리적인 분산투자 관점에서 대략적인 비중을 제시하라. "
            f"종목코드 목록: {', '.join(symbols)}\n"
            "weights의 키는 반드시 위 종목코드와 정확히 일치해야 하고, 값의 합은 1.0에 가까워야 한다."
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_WeightProposalSchema,
                    temperature=0.3,
                ),
            )
            parsed: _WeightProposalSchema = response.parsed
            if parsed is None:
                raise ValueError("빈 응답")
            weights = {item.symbol: item.weight for item in parsed.weights if item.symbol in symbols}
            if not weights:
                raise ValueError("응답에 요청한 종목이 하나도 없음")
            return {"weights": weights, "rationale": parsed.rationale}
        except Exception:
            logger.exception("목표비중 제안 실패 - 균등비중으로 대체 제안")
            equal = 1.0 / len(symbols) if symbols else 0.0
            return {
                "weights": {s: equal for s in symbols},
                "rationale": "LLM 제안 실패 - 임시로 동일비중을 대신 제안함 (검토 후 직접 조정 권장)",
            }
