# tests/test_etf_intelligence.py
import asyncio
from unittest.mock import AsyncMock, patch
import aiosqlite

from core.news.etf_mapper import get_etf_profile, load_etf_map
from core.news import sentiment


def test_etf_profile_resolution():
    async def run():
        etf_map = load_etf_map()
        assert "TSLW" in etf_map
        assert etf_map["TSLW"]["type"] == "SINGLE_STOCK"
        assert etf_map["TSLW"]["underlying"] == "TSLA"

        assert "OZEM" in etf_map
        assert etf_map["OZEM"]["type"] == "THEMATIC"
        assert "LLY" in etf_map["OZEM"]["anchors"]
        assert "NVO" in etf_map["OZEM"]["anchors"]

        assert "SCHD" in etf_map
        assert etf_map["SCHD"]["type"] == "BROAD_INDEX"
        assert etf_map["SCHD"]["macro_driven"] is True

        # Test get_etf_profile
        p_tslw = await get_etf_profile(None, "tslw")
        assert p_tslw is not None
        assert p_tslw["type"] == "SINGLE_STOCK"
        assert p_tslw["underlying"] == "TSLA"

        p_ozem = await get_etf_profile(None, "OZEM")
        assert p_ozem is not None
        assert p_ozem["type"] == "THEMATIC"

        p_schd = await get_etf_profile(None, "SCHD")
        assert p_schd is not None
        assert p_schd["type"] == "BROAD_INDEX"

        # Regular stock should return None
        p_regular = await get_etf_profile(None, "005930")
        assert p_regular is None

    asyncio.run(run())


def test_single_stock_etf_inherits_underlying_sentiment():
    async def run():
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        mock_provider = AsyncMock()

        with patch.object(
            sentiment,
            "fetch_recent_news",
            return_value=[{"url": "u1", "title": "테슬라 실적 서프라이즈", "summary": "s", "published_at": 1780000000.0, "source": "naver"}]
        ), patch(
            "core.db.get_cached_news_sentiment",
            return_value={"sentiment_score": 0.85, "sentiment_reasoning": "실적 서프라이즈"}
        ):
            result = await sentiment.get_symbol_sentiment(mock_conn, mock_provider, "TSLW")
            assert result is not None
            assert result["direction"] == "BUY"
            assert result["strength"] > 0
            assert "etf_proxy" in result
            assert result["etf_proxy"]["etf_symbol"] == "TSLW"
            assert result["etf_proxy"]["type"] == "SINGLE_STOCK"
            assert result["etf_proxy"]["underlying"] == "TSLA"

    asyncio.run(run())


def test_thematic_etf_calculates_anchor_average():
    async def run():
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        mock_provider = AsyncMock()

        # OZEM has anchors: LLY and NVO
        with patch("core.db.get_cached_news_sentiment") as mock_cache:
            mock_cache.return_value = {"sentiment_score": 0.80, "sentiment_reasoning": "비만치료제 호실적"}
            with patch.object(
                sentiment,
                "fetch_recent_news",
                return_value=[{"url": "u_ozem", "title": "GLP-1 매출 급증", "summary": "s", "published_at": 1780000000.0, "source": "naver"}]
            ):
                result = await sentiment.get_symbol_sentiment(mock_conn, mock_provider, "OZEM")
                assert result is not None
                assert result["direction"] == "BUY"
                assert "etf_proxy" in result
                assert result["etf_proxy"]["type"] == "THEMATIC"
                assert "LLY" in result["etf_proxy"]["anchors"]
                assert "NVO" in result["etf_proxy"]["anchors"]

    asyncio.run(run())


def test_broad_index_etf_returns_none_for_news():
    async def run():
        mock_conn = AsyncMock(spec=aiosqlite.Connection)
        mock_provider = AsyncMock()

        # SCHD should bypass news sentiment completely
        result = await sentiment.get_symbol_sentiment(mock_conn, mock_provider, "SCHD")
        assert result is None

        # SPY should bypass news sentiment completely
        result_spy = await sentiment.get_symbol_sentiment(mock_conn, mock_provider, "SPY")
        assert result_spy is None

    asyncio.run(run())

