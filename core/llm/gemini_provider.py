# core/llm/gemini_provider.py
"""Google Gemini API를 실제로 호출하는 LLMProvider 구현체.

모든 호출은 response_schema로 구조화된 JSON을 강제해 파싱 실패 가능성을 줄이지만,
그래도 실패하면(네트워크 오류, 스키마 위반, 타임아웃 등) 절대 예외를 상위로 올리지
않고 안전한 HOLD로 귀결시킨다 — 자동매매 로직이 LLM 장애로 오작동하면 안 되기 때문.
"""
import logging
from typing import Any, Dict, List, Optional, Type, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from core.config import settings
from core.llm.base import LLMProvider

logger = logging.getLogger(__name__)

SAFE_HOLD: Dict[str, Any] = {"direction": "HOLD", "strength": 0.0, "reasoning": ""}

_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


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


class _AddItem(BaseModel):
    symbol: str = Field(description="신규 편입 후보 종목코드 (반드시 candidate_pool 목록 안에서만)")
    name: str = Field(description="종목명 (candidate_pool에 주어진 이름과 정확히 일치해야 함)")
    rationale: str = Field(description="한국어 1문장 편입 근거")


class _RemoveItem(BaseModel):
    symbol: str = Field(description="제외할 기존 보유 종목코드")
    rationale: str = Field(description="한국어 1문장 제외 근거")


class _PortfolioChangeSchema(BaseModel):
    adds: List[_AddItem] = Field(description="신규 편입 제안 목록. 후보가 없으면 반드시 빈 배열")
    removes: List[_RemoveItem] = Field(description="제외 제안 목록. 없으면 빈 배열")
    weights: List[_WeightItem] = Field(
        description="adds/removes 반영 후 전체 목표비중. 합은 1.0을 넘으면 안 되며, 확신이 "
        "부족하면 나머지는 현금으로 남겨도 된다(전액 투자 강제 아님)"
    )
    rationale: str = Field(description="한국어 2~3문장 포트폴리오 전체 근거")


class GeminiProvider(LLMProvider):
    def __init__(self):
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다.")
        self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self._models = [settings.GEMINI_MODEL]
        if settings.GEMINI_FALLBACK_MODEL and settings.GEMINI_FALLBACK_MODEL != settings.GEMINI_MODEL:
            self._models.append(settings.GEMINI_FALLBACK_MODEL)

    async def _generate(
        self, prompt: str, response_schema: Type[_SchemaT], temperature: float
    ) -> _SchemaT:
        """주 모델로 시도하고, 5xx(수요 폭주 등 서버측 장애)만 백업 모델로 한 번 더 시도한다.

        스키마 위반 등 4xx성 오류는 모델을 바꿔도 똑같이 재현될 가능성이 높으므로 재시도하지
        않고 즉시 호출부의 안전한 폴백(HOLD/균등비중)으로 넘긴다.
        """
        last_exc: Exception = RuntimeError("모델 목록이 비어 있음")
        for i, model in enumerate(self._models):
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=response_schema,
                        temperature=temperature,
                    ),
                )
                if response.parsed is None:
                    raise ValueError("빈 응답")
                return response.parsed
            except genai_errors.ServerError as e:
                last_exc = e
                if i < len(self._models) - 1:
                    logger.warning(
                        f"모델 '{model}' 서버 오류({e.code}) - 백업 모델 "
                        f"'{self._models[i + 1]}'로 재시도"
                    )
        raise last_exc

    async def analyze_news(self, symbol: str, title: str, summary: str) -> Dict[str, Any]:
        prompt = (
            f"다음은 종목코드 {symbol}에 관한 뉴스다. 이 뉴스가 이 종목의 주가에 미칠 영향을 "
            f"분석하라. 확실하지 않으면 반드시 HOLD와 낮은 strength를 반환하라 "
            f"(과도한 확신을 가지지 말 것).\n\n제목: {title}\n요약: {summary}"
        )
        try:
            parsed = await self._generate(prompt, _NewsSentimentSchema, temperature=0.2)
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
            parsed = await self._generate(prompt, _WeightProposalSchema, temperature=0.3)
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

    async def propose_portfolio_changes(
        self,
        *,
        current_positions: List[Dict[str, Any]],
        current_signals: Dict[str, Dict[str, Any]],
        candidate_pool: List[Dict[str, Any]],
        macro_context: Optional[str],
        max_symbols: int,
    ) -> Dict[str, Any]:
        current_weights = {p["symbol"]: p["weight"] for p in current_positions}
        candidate_symbols = {c["symbol"] for c in candidate_pool}

        lines = [
            "너는 개인 투자자의 포트폴리오 매니저다. 이 제안은 사람이 검토 후 승인해야만 "
            "실제로 적용된다는 점을 감안해 방어적으로 판단하라. 아래 후보 목록에 없는 "
            "종목은 절대 adds에 포함시키지 마라 (목록 밖 종목코드는 존재하지 않는 것으로 간주).",
            "",
            "[현재 포트폴리오]",
        ]
        for p in current_positions:
            signal = current_signals.get(p["symbol"])
            metrics_parts = []
            if "roi_pct" in p:
                metrics_parts.append(f"내 평단가 대비 ROI {p['roi_pct']:+.1f}%")
            if "days_held" in p:
                metrics_parts.append(f"보유 {p['days_held']:.0f}일")
            if "cagr_pct" in p:
                metrics_parts.append(f"CAGR {p['cagr_pct']:+.1f}%")
            if "mdd_pct" in p:
                metrics_parts.append(f"최근 90일 MDD {p['mdd_pct']:.1f}%")
            if "volatility_pct" in p:
                metrics_parts.append(f"연환산 변동성 {p['volatility_pct']:.1f}%")
            if "sharpe" in p:
                metrics_parts.append(f"샤프 {p['sharpe']:.2f}")
            if "sortino" in p:
                metrics_parts.append(f"소티노 {p['sortino']:.2f}")
            if "beta" in p:
                metrics_parts.append(f"베타(KODEX200 대비) {p['beta']:.2f}")
            if "rsi" in p:
                metrics_parts.append(f"RSI {p['rsi']:.0f}")
            if "macd_cross" in p:
                metrics_parts.append(f"MACD {p['macd_cross']}크로스")
            if "cci" in p:
                metrics_parts.append(f"CCI {p['cci']:.0f}")
            if "bb_percent_b" in p:
                metrics_parts.append(f"볼린저%B {p['bb_percent_b']:.2f}")
            lines.append(
                f"- {p['symbol']}: 비중 {p['weight']:.1%}, 평단가 {p.get('avg_price', 0):.0f}"
                + (f", {', '.join(metrics_parts)}" if metrics_parts else "")
                + (f", 현재시그널 {signal}" if signal else "")
            )
        if candidate_pool:
            lines.append("")
            lines.append("[신규 편입 후보]")
            for c in candidate_pool:
                lines.append(f"- {c['symbol']} ({c.get('name', '')}): {c}")
        else:
            lines.append("")
            lines.append("[신규 편입 후보] 없음 - adds는 반드시 빈 배열로 응답하라.")
        if macro_context:
            lines.append("")
            lines.append(f"[참고 시장 맥락] {macro_context}")
        lines.append("")
        lines.append(
            f"[제약] 최종 포트폴리오 종목 수는 최대 {max_symbols}개. weights의 키는 반드시 "
            "현재 포트폴리오 종목코드 또는 후보 종목코드와 정확히 일치해야 하고, 값의 합은 "
            "1.0을 넘으면 안 된다 (전액을 다 투자할 필요는 없다 - 확신이 부족하면 나머지는 "
            "현금으로 남겨두는 것이 안전하다)."
        )
        prompt = "\n".join(lines)

        try:
            parsed = await self._generate(prompt, _PortfolioChangeSchema, temperature=0.3)
            allowed_symbols = set(current_weights) | candidate_symbols
            weights = {item.symbol: item.weight for item in parsed.weights if item.symbol in allowed_symbols}
            if not weights:
                raise ValueError("응답에 유효한 종목이 하나도 없음")
            adds = [
                {"symbol": a.symbol, "name": a.name, "rationale": a.rationale}
                for a in parsed.adds if a.symbol in candidate_symbols
            ]
            removes = [
                {"symbol": r.symbol, "rationale": r.rationale}
                for r in parsed.removes if r.symbol in current_weights
            ]
            return {
                "adds": adds,
                "removes": removes,
                "weights": weights,
                "rationale": parsed.rationale,
                "degraded": False,
                "error_kind": None,
            }
        except Exception as e:
            logger.exception("포트폴리오 리밸런싱 제안 실패 - 무변경으로 대체")
            return {
                "adds": [],
                "removes": [],
                "weights": dict(current_weights),
                "rationale": "LLM 제안 실패 - 기존 비중을 그대로 유지함 (검토 후 직접 조정 권장)",
                "degraded": True,
                "error_kind": "schema_violation" if isinstance(e, ValueError) else "llm_error",
            }
