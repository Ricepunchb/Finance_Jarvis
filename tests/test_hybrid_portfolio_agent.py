import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from core import db, portfolio_agent
from core.llm.base import LLMProvider


class CaptureLLMProvider(LLMProvider):
    def __init__(self):
        self.last_kwargs = {}

    async def analyze_news(self, symbol: str, title: str, summary: str):
        return {"direction": "HOLD", "strength": 0.0, "reasoning": ""}

    async def propose_weights(self, symbols):
        return {"weights": {}, "rationale": ""}

    async def propose_portfolio_changes(self, **kwargs):
        self.last_kwargs = kwargs
        return {
            "adds": [],
            "removes": [],
            "weights": {p["symbol"]: p["weight"] for p in kwargs.get("current_positions", [])},
            "rationale": "시맨틱과 퀀트 맥락을 고려하여 기존 비중 유지",
            "degraded": False,
            "error_kind": None,
        }

    async def analyze_research_reports(self, reports):
        return {}

    async def summarize_insight_batch(self, context):
        return {}


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    await db._migrate_add_missing_columns(conn)
    return conn


def test_semantic_profile_and_rebalance_integration():
    async def run():
        conn = await _conn()
        now = time.time()

        # 1. 활성 종목 및 포지션 등록
        await db.add_portfolio_symbol(conn, "005930", market="domestic")
        p_id = await db.propose_target_weight(conn, "005930", 0.3, "manual")
        await db.decide_target_weight(conn, p_id, "APPROVED")
        await db.upsert_position(conn, "005930", qty=10, avg_price=70000)

        # 2. 뉴스 감성 캐시 적재
        await conn.execute(
            "INSERT INTO news_cache(symbol, source, url, published_at, raw_text, sentiment_score, sentiment_reasoning, fetched_at) "
            "VALUES ('005930', 'naver', 'http://news/1', ?, 'HBM 수주 확대', 0.85, '강한 실적 호재', ?)",
            (now, now),
        )

        # 3. 증권사 리포트 및 분석 적재
        await db.upsert_research_reports(conn, [{
            "research_id": 101,
            "symbol": "005930",
            "name": "삼성전자",
            "broker": "신한투자",
            "title": "목표가 상향",
            "write_date": "20261005",
            "read_count": 500,
        }])
        await db.save_report_analysis(conn, 101, json.dumps({
            "stance": "POSITIVE",
            "summary": "D램 가격 반등 및 HBM3E 공급 가속",
        }))

        # 4. 인사이트 배치 및 다이제스트 적재
        b_id = await db.create_insight_batch(conn, trigger_type="SCHEDULED", started_at=now)
        digest = {
            "headline": "반도체 주도 강세장",
            "themes": [{"theme": "AI반도체", "evidence": "수출 호조"}],
            "risks": ["원달러 환율 급등"],
            "holdings_watch": ["삼성전자 외국인 수급 지속 여부"],
        }
        await db.finish_insight_batch(conn, b_id, status="DONE", finished_at=now + 1, digest_json=json.dumps(digest))

        # DB 시맨틱 헬퍼 검증
        prof = await db.get_symbol_semantic_profile(conn, "005930")
        assert prof["news_count"] == 1
        assert prof["avg_news_sentiment"] == pytest.approx(0.85)
        assert prof["latest_report_stance"] == "POSITIVE"

        latest_digest = await db.get_latest_insight_digest(conn)
        assert latest_digest is not None
        assert latest_digest["headline"] == "반도체 주도 강세장"

        # 5. 리밸런싱 에이전트 실행 및 LLM 주입 컨텍스트 검증
        fake_llm = CaptureLLMProvider()
        with patch("core.portfolio_agent.get_llm_provider", return_value=fake_llm), \
             patch("core.macro.get_fear_greed_context", new_callable=AsyncMock) as mock_macro:
            mock_macro.return_value = "Fear & Greed: 65 (Greed)"
            res = await portfolio_agent.propose_rebalance(conn, client=None, trigger_type="MANUAL")
            assert res["status"] == "PROPOSED"

            # LLM에 주입된 파라미터 확인
            k = fake_llm.last_kwargs
            assert "005930" in k["current_signals"]
            assert "최근뉴스감성 +0.85" in k["current_signals"]["005930"]
            assert "리포트 POSITIVE" in k["current_signals"]["005930"]

            assert k["insight_digest"] is not None
            assert k["insight_digest"]["headline"] == "반도체 주도 강세장"
            assert k["insight_digest"]["risks"] == ["원달러 환율 급등"]

        await conn.close()

    asyncio.run(run())


def test_gemini_provider_propose_portfolio_changes_with_digest():
    async def run():
        from core.llm.gemini_provider import GeminiProvider, _PortfolioChangeSchema, _WeightItem

        provider = GeminiProvider()
        mock_response = _PortfolioChangeSchema(
            adds=[],
            removes=[],
            weights=[_WeightItem(symbol="005930", weight=0.3)],
            rationale="테마 및 리스크 고려 완료",
        )

        with patch.object(provider, "_generate", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = mock_response
            res = await provider.propose_portfolio_changes(
                current_positions=[{"symbol": "005930", "weight": 0.3}],
                current_signals={"005930": "최근뉴스감성 +0.85, 리포트 POSITIVE"},
                candidate_pool=[{
                    "symbol": "000660",
                    "name": "SK하이닉스",
                    "angle_label": "모멘텀",
                    "score": 0.88,
                    "semantic_score": 0.75,
                    "semantic_penalty": 0.0,
                    "thesis": ["HBM 선도", "외인 수급"],
                }],
                macro_context="Fear & Greed: 60 (Greed)",
                insight_digest={
                    "headline": "반도체 랠리",
                    "themes": [{"theme": "AI인프라", "evidence": "데이터센터 확장"}],
                    "risks": ["고환율"],
                    "holdings_watch": ["수주 확인"],
                },
                max_symbols=5,
                min_weight=0.05,
                max_weight=0.40,
            )
            assert res["rationale"] == "테마 및 리스크 고려 완료"
            prompt_used = mock_gen.call_args[0][0]
            assert "반도체 랠리" in prompt_used
            assert "AI인프라" in prompt_used
            assert "고환율" in prompt_used
            assert "보유종목 관전 포인트" in prompt_used
            assert "AI시맨틱 +0.75" in prompt_used

    asyncio.run(run())

