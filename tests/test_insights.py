import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from core import db
from core.discovery_sources import extract_matched_headlines
from core.fundamentals.naver_research import clean_report_html
from core.insights import InsightCollector, build_batch, generate_batch_digest
from core.llm.base import LLMProvider


class FakeLLMProvider(LLMProvider):
    def __init__(self):
        self.analyze_reports_calls = 0
        self.summarize_digest_calls = 0

    async def analyze_news(self, symbol: str, title: str, summary: str):
        return {"direction": "HOLD", "strength": 0.0, "reasoning": ""}

    async def propose_weights(self, symbols):
        return {"weights": {}, "rationale": ""}

    async def propose_portfolio_changes(self, **kwargs):
        return {"adds": [], "removes": [], "weights": {}, "rationale": "", "degraded": False, "error_kind": None}

    async def analyze_research_reports(self, reports):
        self.analyze_reports_calls += 1
        return {
            r["research_id"]: {
                "stance": "POSITIVE",
                "summary": f"{r.get('name')} 실적 개선 기대",
                "key_points": ["포인트1", "포인트2"],
                "catalysts": ["촉매1"],
                "risks": ["리스크1"],
            }
            for r in reports
        }

    async def summarize_insight_batch(self, context):
        self.summarize_digest_calls += 1
        return {
            "headline": "오늘의 시장 핵심 다이제스트",
            "themes": [{"theme": "반도체", "evidence": "수출 호조", "symbols": ["삼성전자"]}],
            "notable_symbols": [{"symbol": "005930", "name": "삼성전자", "why": "호실적", "stance": "POSITIVE"}],
            "risks": ["환율 변동성"],
            "holdings_watch": ["보유 종목 점검"],
        }


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    return conn


def test_clean_report_html():
    raw = "<p><strong>2026년을 저점으로</strong></p><p><br>지난 9월 29일 IR을 통해 &amp; 중장기 성장</p>"
    cleaned = clean_report_html(raw)
    assert "<" not in cleaned
    assert "&amp;" not in cleaned
    assert "2026년을 저점으로" in cleaned
    assert "& 중장기 성장" in cleaned


def test_extract_matched_headlines():
    headlines = [
        {"hts_pbnt_titl_cntt": "삼성전자 신제품 발표 및 글로벌 점유율 확대", "iscd1": ""},
        {"hts_pbnt_titl_cntt": "현대차와 현대차증권 동반 상승세", "iscd1": ""},
        {"hts_pbnt_titl_cntt": "오늘 날씨 맑음", "iscd1": ""},
    ]
    names = {"005930": "삼성전자", "005380": "현대차", "001500": "현대차증권"}
    matches = extract_matched_headlines(headlines, names)
    assert len(matches) == 2
    matched_syms = {m["symbol"] for m in matches}
    assert "005930" in matched_syms
    assert "001500" in matched_syms or "005380" in matched_syms


def test_insight_batches_db_crud_and_retention():
    async def run():
        conn = await _conn()
        now = time.time()
        # 1. 묶음 생성
        b_id = await db.create_insight_batch(conn, trigger_type="MANUAL", started_at=now)
        assert b_id == 1

        # 2. 아이템 추가
        items = [
            {
                "batch_id": b_id,
                "kind": "broker_report",
                "symbol": "005930",
                "name": "삼성전자",
                "title": "HBM 순항",
                "source": "신한투자",
                "url": "https://m.stock.naver.com/...",
                "published_at": now,
                "ref_id": "99901",
                "is_new": True,
                "payload_json": json.dumps({"target_price": 100000}),
            }
        ]
        await db.add_insight_items(conn, items)

        # 3. 완료 갱신
        await db.finish_insight_batch(
            conn,
            batch_id=b_id,
            status="DONE",
            finished_at=now + 5,
            counts_json=json.dumps({"broker_report": 1, "new_report": 1}),
            digest_json=json.dumps({"headline": "요약"}),
        )

        batches = await db.list_insight_batches(conn)
        assert len(batches) == 1
        assert batches[0]["status"] == "DONE"

        loaded_items = await db.list_insight_items(conn, b_id)
        assert len(loaded_items) == 1
        assert loaded_items[0]["symbol"] == "005930"

        # 4. 1년 보관 기간 정리 테스트 (400일 전 묶음 생성)
        old_id = await db.create_insight_batch(conn, trigger_type="SCHEDULED", started_at=now - 400 * 86400)
        await db.add_insight_items(conn, [{"batch_id": old_id, "kind": "candidate", "symbol": "000660"}])

        try:
            cleaned = await db.cleanup_old_insights(conn, retention_days=365)
            assert cleaned == 1
            batches_after = await db.list_insight_batches(conn)
            assert len(batches_after) == 1
            assert batches_after[0]["id"] == b_id
        finally:
            await conn.close()

    asyncio.run(run())


def test_insights_api_endpoints():
    from fastapi.testclient import TestClient
    from api.main import app

    async def setup_data():
        conn = await _conn()
        now = time.time()
        b_id = await db.create_insight_batch(conn, trigger_type="SCHEDULED", started_at=now)
        await db.add_insight_items(
            conn,
            [
                {
                    "batch_id": b_id,
                    "kind": "broker_report",
                    "symbol": "005930",
                    "name": "삼성전자",
                    "title": "HBM3E 공급",
                    "source": "미래에셋",
                    "url": "https://...",
                    "published_at": now,
                    "ref_id": "1001",
                    "is_new": True,
                    "payload_json": json.dumps({"target_price": 95000, "analysis": {"stance": "POSITIVE"}}),
                }
            ],
        )
        await db.finish_insight_batch(
            conn,
            batch_id=b_id,
            status="DONE",
            finished_at=now + 2,
            counts_json=json.dumps({"broker_report": 1, "new_report": 1}),
            digest_json=json.dumps({"headline": "반도체 랠리 지속"}),
        )
        return conn, b_id

    async def run():
        conn, b_id = await setup_data()
        orig_close = conn.close
        conn.close = AsyncMock()
        try:
            with patch("core.db.get_connection", return_value=conn):
                client = TestClient(app)

                # 1. GET /insights/batches
                res = client.get("/insights/batches")
                assert res.status_code == 200
                data = res.json()
                assert len(data) >= 1
                assert data[0]["id"] == b_id
                assert data[0]["counts"]["broker_report"] == 1

                # 2. GET /insights/batches/{batch_id}
                res = client.get(f"/insights/batches/{b_id}")
                assert res.status_code == 200
                b_detail = res.json()
                assert b_detail["id"] == b_id
                assert len(b_detail["items"]["broker_report"]) == 1
                assert b_detail["items"]["broker_report"][0]["symbol"] == "005930"
                assert b_detail["items"]["broker_report"][0]["payload"]["target_price"] == 95000

                # 3. GET /insights/symbols/{symbol}
                res = client.get("/insights/symbols/005930")
                assert res.status_code == 200
                s_items = res.json()
                assert len(s_items) == 1
                assert s_items[0]["symbol"] == "005930"

                # 4. POST /insights/batches/{batch_id}/digest
                fake_llm = FakeLLMProvider()
                with patch("core.llm.factory.get_llm_provider", return_value=fake_llm):
                    res = client.post(f"/insights/batches/{b_id}/digest")
                    assert res.status_code == 200
                    d_res = res.json()
                    assert d_res["status"] == "ok"
                    assert d_res["digest"]["headline"] == "오늘의 시장 핵심 다이제스트"
        finally:
            await conn.close()

    asyncio.run(run())


def test_build_batch_with_fake_llm():
    async def run():
        conn = await _conn()
        fake_llm = FakeLLMProvider()

        collector = InsightCollector(
            reports=[
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "broker": "하나증권",
                    "title": "HBM 견조",
                    "date": "20261005",
                    "research_id": 1001,
                    "end_url": "https://...",
                }
            ],
            headline_matches=[
                {"title": "삼성전자 반도체 수출 호조", "symbol": "005930", "name": "삼성전자", "matched_symbols": ["005930"]}
            ],
            sampled_headline_count=100,
        )

        scored_entries = [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "universe_tag": "KOSPI_LARGE_CAP",
                "score": 0.85,
                "angle": "momentum",
                "angle_label": "모멘텀",
                "thesis": ["외국인 매수세", "실적 턴어라운드"],
                "sources": {"broker": {}},
            }
        ]

        try:
            with patch("core.insights.get_llm_provider", return_value=fake_llm), \
                 patch("core.insights.fetch_research_detail", return_value={
                     "research_id": 1001,
                     "content_text": "본문 내용: 3분기 영업이익이 크게 증가했습니다.",
                     "opinion": "매수",
                     "target_price": 95000.0,
                     "price_at_write": 72000.0,
                     "attach_url": "https://...pdf",
                 }):
                res = await build_batch(
                    conn, collector, scored_entries, refresh_summary={"upserted": 1}, trigger_type="MANUAL"
                )

            assert res["status"] == "DONE"
            assert res["counts"]["broker_report"] == 1
            assert res["counts"]["market_headline"] == 1
            assert res["counts"]["candidate"] == 1
            assert fake_llm.analyze_reports_calls == 1
            assert fake_llm.summarize_digest_calls == 1

            batch = await db.get_insight_batch(conn, res["batch_id"])
            digest = json.loads(batch["digest_json"])
            assert digest["headline"] == "오늘의 시장 핵심 다이제스트"

            # 다시 한 번 build_batch를 호출했을 때 이미 분석된 리포트는 다시 LLM 호출하지 않는지 검증
            with patch("core.insights.get_llm_provider", return_value=fake_llm), \
                 patch("core.insights.fetch_research_detail") as mock_fetch:
                res2 = await build_batch(
                    conn, collector, scored_entries, refresh_summary={"upserted": 1}, trigger_type="MANUAL"
                )
                # 이미 본문과 분석이 캐시되어 있으므로 detail fetch와 analyze 호출이 추가되지 않아야 함
                assert mock_fetch.call_count == 0
                assert fake_llm.analyze_reports_calls == 1  # 여전히 1회 유지
        finally:
            await conn.close()

    asyncio.run(run())
