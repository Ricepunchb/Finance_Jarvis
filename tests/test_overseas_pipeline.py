# tests/test_overseas_pipeline.py
"""해외 종목 AI 리서치, 뉴스, 컨센서스 수집, 발굴/점수화 및 트레이딩 시그널 연계 테스트."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from core import db, signal_engine
from core.config import get_effective_domestic_only, settings
from core.discovery import validate_candidate_symbol
from core.fundamentals import overseas_consensus, valuation, yfinance_client
from core.news import naver_finance
from core.utils import symbol_mapper


# --- 1. Symbol Mapper 테스트 ---
def test_symbol_mapper_is_overseas():
    assert symbol_mapper.is_overseas_symbol("AAPL") is True
    assert symbol_mapper.is_overseas_symbol("VST") is True
    assert symbol_mapper.is_overseas_symbol("GLD") is True
    assert symbol_mapper.is_overseas_symbol("005930") is False
    assert symbol_mapper.is_overseas_symbol("000660") is False


def test_symbol_mapper_reuters_conversion():
    assert symbol_mapper.ticker_to_reuters_code("AAPL") == "AAPL.O"
    assert symbol_mapper.ticker_to_reuters_code("NVDA") == "NVDA.O"
    assert symbol_mapper.ticker_to_reuters_code("VST") == "VST"
    assert symbol_mapper.ticker_to_reuters_code("GLD") == "GLD"
    assert symbol_mapper.ticker_to_reuters_code("LLY") == "LLY"
    assert symbol_mapper.reuters_code_to_ticker("AAPL.O") == "AAPL"
    assert symbol_mapper.reuters_code_to_ticker("VST") == "VST"


# --- 2. yfinance Client 테스트 ---
def test_yfinance_fetch_targets():
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_instance = MagicMock()
        mock_instance.info = {
            "targetMeanPrice": 250.0,
            "targetHighPrice": 300.0,
            "targetLowPrice": 200.0,
            "recommendationMean": 2.1,
            "currentPrice": 220.0,
            "fiftyTwoWeekHigh": 240.0,
            "fiftyTwoWeekLow": 160.0,
            "trailingPE": 30.5,
            "priceToBook": 15.2,
        }
        mock_ticker_cls.return_value = mock_instance

        targets = yfinance_client.fetch_analyst_targets("AAPL")
        assert targets["symbol"] == "AAPL"
        assert targets["target_price_mean"] == 250.0
        assert targets["last_close"] == 220.0
        assert targets["per"] == 30.5
        assert targets["pbr"] == 15.2
        assert targets["recomm_mean"] == 2.1


def test_yfinance_fetch_news_fallback():
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_instance = MagicMock()
        mock_instance.news = [
            {
                "content": {
                    "title": "Apple unveils new M4 chips",
                    "summary": "Apple announced new lineup...",
                    "canonicalUrl": {"url": "https://finance.yahoo.com/news/apple-m4"},
                    "pubDate": 1700000000.0,
                }
            }
        ]
        mock_ticker_cls.return_value = mock_instance

        news = yfinance_client.fetch_recent_news_yfinance("AAPL", max_items=2)
        assert len(news) == 1
        assert news[0]["symbol"] == "AAPL"
        assert news[0]["source"] == "yfinance_news"
        assert news[0]["title"] == "Apple unveils new M4 chips"
        assert news[0]["published_at"] == 1700000000.0


# --- 3. Overseas Consensus & Research 수집 테스트 ---
def test_overseas_consensus_parsing():
    with patch("requests.get") as mock_get:
        # consensus API 응답
        resp_consensus = MagicMock()
        resp_consensus.status_code = 200
        resp_consensus.text = '{"priceTargetMean": "250.50", "recommMean": "2.2"}'
        resp_consensus.json.return_value = {"priceTargetMean": "250.50", "recommMean": "2.2"}

        # basic API 응답
        resp_basic = MagicMock()
        resp_basic.status_code = 200
        resp_basic.text = '{"closePrice": "200.00", "stockItemTotalInfos": [{"code": "highPriceOf52Weeks", "value": "240.00"}, {"code": "lowPriceOf52Weeks", "value": "150.00"}, {"code": "per", "value": "28.5배"}, {"code": "pbr", "value": "12.0배"}]}'
        resp_basic.json.return_value = {
            "closePrice": "200.00",
            "stockItemTotalInfos": [
                {"code": "highPriceOf52Weeks", "value": "240.00"},
                {"code": "lowPriceOf52Weeks", "value": "150.00"},
                {"code": "per", "value": "28.5배"},
                {"code": "pbr", "value": "12.0배"},
            ],
        }

        # research API 응답
        resp_research = MagicMock()
        resp_research.status_code = 200
        resp_research.text = '[{"researchId": "R001", "title": "Apple Morningstar Report", "analystNotePublishDate": "2026.10.01", "fairValue": "230.00", "economicMoatType": {"name": "넓음"}, "originalPDF": "http://pdf"}]'
        resp_research.json.return_value = [
            {
                "researchId": "R001",
                "title": "Apple Morningstar Report",
                "analystNotePublishDate": "2026.10.01",
                "fairValue": "230.00",
                "economicMoatType": {"name": "넓음"},
                "originalPDF": "http://pdf",
            }
        ]

        def side_effect(url, **kwargs):
            if "consensus" in url:
                return resp_consensus
            elif "basic" in url:
                return resp_basic
            elif "research" in url:
                return resp_research
            return MagicMock(status_code=404, text="")

        mock_get.side_effect = side_effect

        data = overseas_consensus.fetch_overseas_consensus("AAPL")
        assert data["symbol"] == "AAPL"
        assert data["target_price_mean"] == 250.50
        assert data["last_close"] == 200.00
        assert data["w52_high"] == 240.00
        assert data["w52_low"] == 150.00
        assert data["per"] == 28.5
        assert data["pbr"] == 12.0
        assert len(data["researches"]) == 1
        assert data["researches"][0]["broker"] == "Morningstar"
        assert data["researches"][0]["fair_value"] == 230.00


# --- 4. Naver Finance 해외 뉴스 라우팅 및 폴백 테스트 ---
def test_fetch_recent_news_overseas_routing():
    with patch("requests.get") as mock_get:
        # 네이버 해외 뉴스 반환 mock
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "ok"
        resp.json.return_value = [
            {
                "items": [
                    {
                        "mobileNewsUrl": "https://m.stock.naver.com/news/1",
                        "titleFull": "테슬라 3분기 인도량 호조",
                        "body": "테슬라가 시장 예상치를 웃도는 인도량을 기록했다.",
                        "datetime": "202610061200",
                    }
                ]
            }
        ]
        mock_get.return_value = resp

        articles = naver_finance.fetch_recent_news("TSLA", max_items=5)
        assert len(articles) == 1
        assert articles[0]["symbol"] == "TSLA"
        assert articles[0]["source"] == "naver_finance_overseas"
        assert "인도량" in articles[0]["title"]


# --- 5. 해외 밸류에이션 시그널 계산 테스트 ---
def test_overseas_valuation_signal_buy():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(db.SCHEMA)

            with patch("core.fundamentals.overseas_consensus.fetch_overseas_consensus") as mock_fetch:
                mock_fetch.return_value = {
                    "symbol": "AAPL",
                    "target_price_mean": 300.0,  # 현재가 200 대비 +50% 괴리
                    "last_close": 200.0,
                    "w52_high": 250.0,
                    "w52_low": 180.0,  # 200이면 저점 쪽에 가까움
                    "per": 25.0,
                    "pbr": 10.0,
                    "recomm_mean": 2.0,
                }

                sig = await valuation.get_valuation_signal_overseas(
                    conn, "AAPL", current_price=200.0
                )
                assert sig is not None
                assert sig["direction"] == "BUY"
                assert sig["strength"] > 0.5
    asyncio.run(run())


# --- 6. Signal Engine 결합 신호 테스트 (해외 감성 + 밸류에이션 결합) ---
def test_signal_engine_combine_overseas():
    tech = {"direction": "BUY", "strength": 0.6}
    intraday = {"direction": "BUY", "strength": 0.8}
    sentiment = {"direction": "BUY", "strength": 0.7}
    valuation_sig = {"direction": "BUY", "strength": 0.9}

    combined = signal_engine.combine_signals(
        tech=tech,
        intraday=intraday,
        sentiment=sentiment,
        valuation=valuation_sig,
    )
    assert combined["direction"] == "BUY"
    assert combined["strength"] >= 0.7


# --- 7. Candidate Symbol Validation (해외 종목 검증) ---
def test_validate_candidate_symbol_overseas():
    async def run():
        mock_client = AsyncMock()

        with patch("core.kis_overseas.get_price") as mock_price:
            mock_price.return_value = {
                "last": "230.50",
                "ovrs_nmix_prpr": "230.50",
                "hts_kor_isnm": "애플",
            }

            outcome = await validate_candidate_symbol(
                mock_client, "AAPL", market="overseas", claimed_name="Apple Inc."
            )
            assert outcome.ok is True
            assert outcome.kis_name == "애플"
    asyncio.run(run())


# --- 8. 동적 domestic_only 토글 테스트 ---
def test_dynamic_domestic_only_toggle():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(db.SCHEMA)

            # 초기 상태: DB state 없으면 settings.DOMESTIC_ONLY (True)
            assert await get_effective_domestic_only(conn) is settings.DOMESTIC_ONLY

            # UI에서 토글 OFF ("0")
            await db.set_state(conn, "domestic_only", "0")
            assert await get_effective_domestic_only(conn) is False

            # UI에서 토글 ON ("1")
            await db.set_state(conn, "domestic_only", "1")
            assert await get_effective_domestic_only(conn) is True
    asyncio.run(run())

