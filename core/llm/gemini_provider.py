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


class _ReportAnalysisItem(BaseModel):
    research_id: int = Field(description="리포트 ID")
    stance: str = Field(description="리포트 어조: POSITIVE, NEUTRAL, NEGATIVE 중 하나")
    summary: str = Field(description="한국어 2문장 핵심 요약")
    key_points: List[str] = Field(description="핵심 분석 포인트 (최대 3개)")
    catalysts: List[str] = Field(description="상승/실적 개선 모멘텀 및 촉매 (최대 2개)")
    risks: List[str] = Field(description="주의해야 할 리스크/우려 요인 (최대 2개)")


class _ReportAnalysisBatchSchema(BaseModel):
    items: List[_ReportAnalysisItem] = Field(description="분석된 리포트 목록")


class _ThemeItem(BaseModel):
    theme: str = Field(description="테마/키워드 명칭")
    evidence: str = Field(description="해당 테마가 부각된 이유/근거")
    symbols: List[str] = Field(description="관련 종목명 또는 종목코드 목록")


class _NotableSymbolItem(BaseModel):
    symbol: str = Field(description="종목코드")
    name: str = Field(description="종목명")
    why: str = Field(description="주목해야 하는 핵심 이유 1~2문장")
    stance: str = Field(description="POSITIVE, NEUTRAL, 또는 CAUTION")


class _InsightDigestSchema(BaseModel):
    headline: str = Field(description="오늘 시장/발굴 종합 한 줄 요약")
    themes: List[_ThemeItem] = Field(description="오늘 주목할 핵심 테마 목록 (최대 5개)")
    notable_symbols: List[_NotableSymbolItem] = Field(description="가장 주목할 종목 목록 (최대 8개)")
    risks: List[str] = Field(description="시장 및 업종 주요 경계 요인 (최대 4개)")
    holdings_watch: List[str] = Field(description="현재 보유 종목과 관련된 관전 포인트 및 주의점")



class _ReportAnalysisItem(BaseModel):
    research_id: int = Field(description="리포트 ID")
    stance: str = Field(description="리포트 어조: POSITIVE, NEUTRAL, NEGATIVE 중 하나")
    summary: str = Field(description="한국어 2문장 핵심 요약")
    key_points: List[str] = Field(description="핵심 분석 포인트 (최대 3개)")
    catalysts: List[str] = Field(description="상승/실적 개선 모멘텀 및 촉매 (최대 2개)")
    risks: List[str] = Field(description="주의해야 할 리스크/우려 요인 (최대 2개)")


class _ReportAnalysisBatchSchema(BaseModel):
    items: List[_ReportAnalysisItem] = Field(description="분석된 리포트 목록")


class _ThemeItem(BaseModel):
    theme: str = Field(description="테마/키워드 명칭")
    evidence: str = Field(description="해당 테마가 부각된 이유/근거")
    symbols: List[str] = Field(description="관련 종목명 또는 종목코드 목록")


class _NotableSymbolItem(BaseModel):
    symbol: str = Field(description="종목코드")
    name: str = Field(description="종목명")
    why: str = Field(description="주목해야 하는 핵심 이유 1~2문장")
    stance: str = Field(description="POSITIVE, NEUTRAL, 또는 CAUTION")


class _InsightDigestSchema(BaseModel):
    headline: str = Field(description="오늘 시장/발굴 종합 한 줄 요약")
    themes: List[_ThemeItem] = Field(description="오늘 주목할 핵심 테마 목록 (최대 5개)")
    notable_symbols: List[_NotableSymbolItem] = Field(description="가장 주목할 종목 목록 (최대 8개)")
    risks: List[str] = Field(description="시장 및 업종 주요 경계 요인 (최대 4개)")
    holdings_watch: List[str] = Field(description="현재 보유 종목과 관련된 관전 포인트 및 주의점")



def _format_candidate(c: Dict[str, Any]) -> str:
    parts = []
    if c.get("angle_label"):
        parts.append(f"관점 {c['angle_label']} (점수 {c.get('score', 0):.2f})")
    if c.get("theme"):
        parts.append(f"테마 {c['theme']}")
    if c.get("per") is not None:
        parts.append(f"PER {c['per']:.1f}")
    if c.get("pbr") is not None:
        parts.append(f"PBR {c['pbr']:.2f}")
    if c.get("target_gap_pct") is not None:
        parts.append(f"목표가 괴리 {c['target_gap_pct']:+.0%}")
    if c.get("news_accel") is not None:
        parts.append(f"뉴스 가속 {c['news_accel']:.1f}배 (최근3일 {c.get('news_3d', 0)}건)")
    if c.get("vol_ratio_5_20") is not None:
        parts.append(f"거래량 5일/20일 {c['vol_ratio_5_20']:.1f}배")
    if c.get("ret_5d_pct") is not None:
        parts.append(f"5일 수익률 {c['ret_5d_pct']:+.1f}%")
    if "period_return_pct" in c:
        parts.append(f"90일 수익률 {c['period_return_pct']:+.1f}%")
    if "mdd_pct" in c:
        parts.append(f"MDD {c['mdd_pct']:.1f}%")
    if "volatility_pct" in c:
        parts.append(f"변동성 {c['volatility_pct']:.1f}%")
    if "rsi" in c:
        parts.append(f"RSI {c['rsi']:.0f}")
    if c.get("tech"):
        parts.append(f"기술적시그널 {c['tech'].get('direction')} {c['tech'].get('strength', 0):.2f}")
    if c.get("semantic_score") is not None:
        parts.append(f"AI시맨틱 {c['semantic_score']:+.2f}")
    elif c.get("raw_components", {}).get("semantic") is not None:
        parts.append(f"AI시맨틱 {c['raw_components']['semantic']:+.2f}")
    if c.get("semantic_penalty"):
        parts.append(f"시맨틱페널티 {c['semantic_penalty']:.2f}")
    line = f"- {c['symbol']} ({c.get('name', '')}): " + ", ".join(parts)
    for reason in c.get("thesis") or []:
        line += f"\n    · {reason}"
    return line


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
        insight_digest: Optional[Dict[str, Any]] = None,
        max_symbols: int,
        min_weight: float,
        max_weight: float,
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
            lines.append(
                "[신규 편입 후보] 관점별로 발굴됨. '하입 조기신호'는 관심 대비 주가 미반영이라는 관측일 뿐 "
                "상승 예측이 아니고, 테마/모멘텀 후보는 변동성이 크다 — 이런 후보를 담는다면 최소 수준의 "
                "비중으로만 담고 rationale에 반대 근거(리스크)도 함께 적어라. 후보에는 ETF(KODEX/TIGER 등 종목명)도 "
                "섞여 있다 — ETF는 PER/PBR/목표가가 없고 지수·테마 분산 수단이므로 개별 종목과 같은 잣대로 비교하지 말고, "
                "기초지수·분산 효과와 이미 보유한 종목과의 중복(같은 지수/섹터를 ETF와 개별주로 이중 보유)을 따져라."
            )
            for c in candidate_pool:
                lines.append(_format_candidate(c))
        else:
            lines.append("")
            lines.append("[신규 편입 후보] 없음 - adds는 반드시 빈 배열로 응답하라.")
        if macro_context:
            lines.append("")
            lines.append(f"[참고 시장 맥락] {macro_context}")
        if insight_digest:
            lines.append("")
            lines.append("[AI 시장 인텔리전스 다이제스트 (참고)]")
            if insight_digest.get("headline"):
                lines.append(f"- 시장 헤드라인: {insight_digest['headline']}")
            if insight_digest.get("themes"):
                th_strs = [f"{t.get('theme')}({t.get('evidence', '')})" for t in insight_digest['themes'] if isinstance(t, dict)]
                if th_strs:
                    lines.append(f"- 핵심 주도 테마: {', '.join(th_strs)}")
            if insight_digest.get("risks"):
                lines.append(f"- 주요 경계 리스크: {', '.join(insight_digest['risks'])}")
            if insight_digest.get("holdings_watch"):
                lines.append(f"- 보유종목 관전 포인트: {', '.join(insight_digest['holdings_watch'])}")
        lines.append("")
        lines.append(
            f"[제약] 최종 포트폴리오 종목 수는 최대 {max_symbols}개. weights의 키는 반드시 "
            "현재 포트폴리오 종목코드 또는 후보 종목코드와 정확히 일치해야 하고, 값의 합은 "
            "1.0을 넘으면 안 된다 (전액을 다 투자할 필요는 없다 - 확신이 부족하면 나머지는 "
            "현금으로 남겨두는 것이 안전하다).\n"
            f"[비중 범위] 보유할 종목(비중>0)의 비중은 반드시 {min_weight:.0%} 이상 {max_weight:.0%} 이하여야 한다. "
            f"이 범위를 벗어난 종목이 하나라도 있으면 제안 전체가 폐기된다. '최소 비중'으로 담으려면 "
            f"{min_weight:.0%}를 쓰고, 그 이하로 담을 만큼의 확신이면 adds에 넣지 마라. "
            "제외하려는 종목은 removes에 넣고 비중을 0으로 둔다."
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

    async def analyze_research_reports(
        self, reports: List[Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        if not reports:
            return {}

        report_blocks = []
        for r in reports:
            rid = r.get("research_id") or r.get("id")
            block = (
                f"[리포트 ID: {rid}]\n"
                f"- 종목: {r.get('symbol')} ({r.get('name', '')})\n"
                f"- 증권사: {r.get('broker', '')}\n"
                f"- 제목: {r.get('title', '')}\n"
                f"- 투자의견: {r.get('opinion') or '미제시'}\n"
                f"- 목표주가: {f'{r.get('target_price'):,.0f}원' if r.get('target_price') else '미제시'}\n"
                f"- 작성일주가: {f'{r.get('price_at_write'):,.0f}원' if r.get('price_at_write') else '미제시'}\n"
                f"- 본문 요약/발췌:\n{(r.get('content_text') or '')[:1200]}"
            )
            report_blocks.append(block)

        prompt = (
            "너는 주식 분석 리서치 센터의 냉철한 퀀트/애널리스트다. "
            "아래 제공된 국내 증권사 리포트들을 각각 분석하라.\n\n"
            "[주의 및 판정 지침]\n"
            "1. 국내 증권사 리포트는 구조적으로 매수 편향(Buy Bias)이 강하다. 따라서 무조건 긍정적으로 보지 말고, "
            "목표주가 하향, 이익 추정치 감소, 업황 둔화 우려, 불확실성 언급이 있다면 stance를 반드시 NEUTRAL 또는 NEGATIVE로 엄정히 판정하라.\n"
            "2. 확실히 호실적 또는 구조적 성장 동력이 명확할 때만 POSITIVE를 부여하라.\n"
            "3. 본문 내용에 근거하지 않은 사실을 지어내지 말라(환각 금지).\n"
            "4. summary는 한국어 2문장으로 핵심을 요약하라.\n\n"
            + "\n\n---\n\n".join(report_blocks)
        )

        try:
            parsed = await self._generate(prompt, _ReportAnalysisBatchSchema, temperature=0.2)
            results = {}
            for item in parsed.items:
                stance = item.stance.upper()
                if stance not in ("POSITIVE", "NEUTRAL", "NEGATIVE"):
                    stance = "NEUTRAL"
                results[item.research_id] = {
                    "stance": stance,
                    "summary": item.summary,
                    "key_points": item.key_points[:3],
                    "catalysts": item.catalysts[:2],
                    "risks": item.risks[:2],
                }
            return results
        except Exception:
            logger.exception("증권사 리포트 배치 LLM 분석 실패 - 빈 결과 반환")
            return {}

    async def summarize_insight_batch(self, context: Dict[str, Any]) -> Dict[str, Any]:
        valid_symbols = set()
        for k in ("candidates", "holding_news", "new_reports", "holdings"):
            for item in context.get(k) or []:
                s = item.get("symbol")
                if s:
                    valid_symbols.add(s)

        lines = [
            "너는 전문 포트폴리오 매니저이자 시장 인텔리전스 분석가다. "
            "오늘 수집·분석된 증권사 리포트, 시장 뉴스 헤드라인 표본, 보유종목 뉴스 감성, 종목 발굴 결과를 바탕으로 "
            "투자자를 위한 일일 종합 다이제스트를 작성하라.\n",
            "[작성 지침]",
            "1. headline: 오늘의 시장/테마/발굴 흐름을 관통하는 명확하고 흥미로운 한 줄 요약 헤드라인을 작성하라.",
            "2. themes: 오늘 리포트/뉴스/급등 종목들에서 강하게 포착된 핵심 테마를 최대 5개 도출하라.",
            "3. notable_symbols: 제공된 종목들 중 오늘 특히 주목할 가치가 있는 종목을 최대 8개 엄선하라.",
            "   (주의: 제공된 자료에 명시된 종목코드와 종목명만 사용해야 하며 절대 존재하지 않는 종목을 환각하지 마라)",
            "4. risks: 시장 전반 또는 주요 섹터에서 포착되는 주의/경계 요인을 최대 4개 정리하라.",
            "5. holdings_watch: 현재 보유 종목과 관련된 관전 포인트 및 주의점을 정리하라.\n",
            "[제공 데이터]",
        ]

        if context.get("holdings"):
            h_str = ", ".join(f"{h.get('symbol')}({h.get('name', '')})" for h in context["holdings"])
            lines.append(f"- 현재 포트폴리오 보유종목: {h_str}")

        if context.get("new_reports"):
            lines.append("\n[오늘 신규/주요 증권사 리포트]")
            for r in context["new_reports"][:15]:
                lines.append(
                    f"· [{r.get('broker')}] {r.get('symbol')}({r.get('name')}) - '{r.get('title')}' "
                    f"(의견: {r.get('opinion') or '-'}, 목표가: {r.get('target_price') or '-'}, 판정: {r.get('stance') or '-'})"
                )

        if context.get("market_headlines"):
            lines.append("\n[시장 전체 뉴스 다빈도 언급 종목 및 헤드라인]")
            for h in context["market_headlines"][:12]:
                lines.append(f"· {h.get('symbol')}({h.get('name')}): {h.get('title')} (언급 {h.get('mentions', 1)}회)")

        if context.get("holding_news"):
            lines.append("\n[보유 종목 최근 뉴스 및 감성 판정]")
            for n in context["holding_news"][:10]:
                lines.append(
                    f"· {n.get('symbol')}: {n.get('title')} (점수: {n.get('score', 0):+.2f}, 사유: {n.get('reasoning', '')[:60]})"
                )

        if context.get("candidates"):
            lines.append("\n[오늘 발굴 상위 후보 종목 (점수순)]")
            for c in context["candidates"][:10]:
                lines.append(
                    f"· {c.get('symbol')}({c.get('name')}) [{c.get('angle_label')} {c.get('score', 0):.2f}점]: "
                    + " / ".join(c.get("thesis") or [])
                )

        prompt = "\n".join(lines)

        fallback = {
            "headline": "오늘의 종목 발굴 및 리포트/뉴스 분석 요약",
            "themes": [],
            "notable_symbols": [],
            "risks": [],
            "holdings_watch": [],
        }

        try:
            parsed = await self._generate(prompt, _InsightDigestSchema, temperature=0.3)
            notables = []
            for ns in parsed.notable_symbols:
                if ns.symbol in valid_symbols or not valid_symbols:
                    notables.append({
                        "symbol": ns.symbol,
                        "name": ns.name,
                        "why": ns.why,
                        "stance": ns.stance.upper() if ns.stance else "POSITIVE",
                    })

            return {
                "headline": parsed.headline,
                "themes": [t.model_dump() for t in parsed.themes[:5]],
                "notable_symbols": notables[:8],
                "risks": parsed.risks[:4],
                "holdings_watch": parsed.holdings_watch[:4],
            }
        except Exception:
            logger.exception("인사이트 다이제스트 LLM 생성 실패 - 기본 폴백 반환")
            return fallback


    async def analyze_research_reports(
        self, reports: List[Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        if not reports:
            return {}

        report_blocks = []
        for r in reports:
            rid = r.get("research_id") or r.get("id")
            block = (
                f"[리포트 ID: {rid}]\n"
                f"- 종목: {r.get('symbol')} ({r.get('name', '')})\n"
                f"- 증권사: {r.get('broker', '')}\n"
                f"- 제목: {r.get('title', '')}\n"
                f"- 투자의견: {r.get('opinion') or '미제시'}\n"
                f"- 목표주가: {f'{r.get('target_price'):,.0f}원' if r.get('target_price') else '미제시'}\n"
                f"- 작성일주가: {f'{r.get('price_at_write'):,.0f}원' if r.get('price_at_write') else '미제시'}\n"
                f"- 본문 요약/발췌:\n{(r.get('content_text') or '')[:1200]}"
            )
            report_blocks.append(block)

        prompt = (
            "너는 주식 분석 리서치 센터의 냉철한 퀀트/애널리스트다. "
            "아래 제공된 국내 증권사 리포트들을 각각 분석하라.\n\n"
            "[주의 및 판정 지침]\n"
            "1. 국내 증권사 리포트는 구조적으로 매수 편향(Buy Bias)이 강하다. 따라서 무조건 긍정적으로 보지 말고, "
            "목표주가 하향, 이익 추정치 감소, 업황 둔화 우려, 불확실성 언급이 있다면 stance를 반드시 NEUTRAL 또는 NEGATIVE로 엄정히 판정하라.\n"
            "2. 확실히 호실적 또는 구조적 성장 동력이 명확할 때만 POSITIVE를 부여하라.\n"
            "3. 본문 내용에 근거하지 않은 사실을 지어내지 말라(환각 금지).\n"
            "4. summary는 한국어 2문장으로 핵심을 요약하라.\n\n"
            + "\n\n---\n\n".join(report_blocks)
        )

        try:
            parsed = await self._generate(prompt, _ReportAnalysisBatchSchema, temperature=0.2)
            results = {}
            for item in parsed.items:
                stance = item.stance.upper()
                if stance not in ("POSITIVE", "NEUTRAL", "NEGATIVE"):
                    stance = "NEUTRAL"
                results[item.research_id] = {
                    "stance": stance,
                    "summary": item.summary,
                    "key_points": item.key_points[:3],
                    "catalysts": item.catalysts[:2],
                    "risks": item.risks[:2],
                }
            return results
        except Exception:
            logger.exception("증권사 리포트 배치 LLM 분석 실패 - 빈 결과 반환")
            return {}

    async def summarize_insight_batch(self, context: Dict[str, Any]) -> Dict[str, Any]:
        valid_symbols = set()
        for k in ("candidates", "holding_news", "new_reports", "holdings"):
            for item in context.get(k) or []:
                s = item.get("symbol")
                if s:
                    valid_symbols.add(s)

        lines = [
            "너는 전문 포트폴리오 매니저이자 시장 인텔리전스 분석가다. "
            "오늘 수집·분석된 증권사 리포트, 시장 뉴스 헤드라인 표본, 보유종목 뉴스 감성, 종목 발굴 결과를 바탕으로 "
            "투자자를 위한 일일 종합 다이제스트를 작성하라.\n",
            "[작성 지침]",
            "1. headline: 오늘의 시장/테마/발굴 흐름을 관통하는 명확하고 흥미로운 한 줄 요약 헤드라인을 작성하라.",
            "2. themes: 오늘 리포트/뉴스/급등 종목들에서 강하게 포착된 핵심 테마를 최대 5개 도출하라.",
            "3. notable_symbols: 제공된 종목들 중 오늘 특히 주목할 가치가 있는 종목을 최대 8개 엄선하라.",
            "   (주의: 제공된 자료에 명시된 종목코드와 종목명만 사용해야 하며 절대 존재하지 않는 종목을 환각하지 마라)",
            "4. risks: 시장 전반 또는 주요 섹터에서 포착되는 주의/경계 요인을 최대 4개 정리하라.",
            "5. holdings_watch: 현재 보유 종목과 관련된 관전 포인트 및 주의점을 정리하라.\n",
            "[제공 데이터]",
        ]

        if context.get("holdings"):
            h_str = ", ".join(f"{h.get('symbol')}({h.get('name', '')})" for h in context["holdings"])
            lines.append(f"- 현재 포트폴리오 보유종목: {h_str}")

        if context.get("new_reports"):
            lines.append("\n[오늘 신규/주요 증권사 리포트]")
            for r in context["new_reports"][:15]:
                lines.append(
                    f"· [{r.get('broker')}] {r.get('symbol')}({r.get('name')}) - '{r.get('title')}' "
                    f"(의견: {r.get('opinion') or '-'}, 목표가: {r.get('target_price') or '-'}, 판정: {r.get('stance') or '-'})"
                )

        if context.get("market_headlines"):
            lines.append("\n[시장 전체 뉴스 다빈도 언급 종목 및 헤드라인]")
            for h in context["market_headlines"][:12]:
                lines.append(f"· {h.get('symbol')}({h.get('name')}): {h.get('title')} (언급 {h.get('mentions', 1)}회)")

        if context.get("holding_news"):
            lines.append("\n[보유 종목 최근 뉴스 및 감성 판정]")
            for n in context["holding_news"][:10]:
                lines.append(
                    f"· {n.get('symbol')}: {n.get('title')} (점수: {n.get('score', 0):+.2f}, 사유: {n.get('reasoning', '')[:60]})"
                )

        if context.get("candidates"):
            lines.append("\n[오늘 발굴 상위 후보 종목 (점수순)]")
            for c in context["candidates"][:10]:
                lines.append(
                    f"· {c.get('symbol')}({c.get('name')}) [{c.get('angle_label')} {c.get('score', 0):.2f}점]: "
                    + " / ".join(c.get("thesis") or [])
                )

        prompt = "\n".join(lines)

        fallback = {
            "headline": "오늘의 종목 발굴 및 리포트/뉴스 분석 요약",
            "themes": [],
            "notable_symbols": [],
            "risks": [],
            "holdings_watch": [],
        }

        try:
            parsed = await self._generate(prompt, _InsightDigestSchema, temperature=0.3)
            notables = []
            for ns in parsed.notable_symbols:
                if ns.symbol in valid_symbols or not valid_symbols:
                    notables.append({
                        "symbol": ns.symbol,
                        "name": ns.name,
                        "why": ns.why,
                        "stance": ns.stance.upper() if ns.stance else "POSITIVE",
                    })

            return {
                "headline": parsed.headline,
                "themes": [t.model_dump() for t in parsed.themes[:5]],
                "notable_symbols": notables[:8],
                "risks": parsed.risks[:4],
                "holdings_watch": parsed.holdings_watch[:4],
            }
        except Exception:
            logger.exception("인사이트 다이제스트 LLM 생성 실패 - 기본 폴백 반환")
            return fallback

