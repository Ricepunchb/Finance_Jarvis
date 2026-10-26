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


def test_is_valid_ticker():
    from core.news.etf_mapper import _is_valid_ticker, _clean_ticker

    assert _is_valid_ticker("NVDA") is True
    assert _is_valid_ticker("TSLA") is True
    assert _is_valid_ticker("005930") is True
    assert _is_valid_ticker("$TSLA") is True
    assert _clean_ticker("$TSLA") == "TSLA"
    assert _clean_ticker(" avgo ") == "AVGO"

    # Words and invalid strings should be rejected
    assert _is_valid_ticker("SEMICONDUCTORS") is False
    assert _is_valid_ticker("CHIPS") is False
    assert _is_valid_ticker("INDEX") is False
    assert _is_valid_ticker("") is False
    assert _is_valid_ticker(None) is False


def test_classify_via_holdings():
    import pandas as pd
    from unittest.mock import MagicMock
    from core.news.etf_mapper import _classify_via_holdings

    # 1. 1위 비중 >= 50% ➔ SINGLE_STOCK
    mock_t1 = MagicMock()
    mock_t1.funds_data.top_holdings = pd.DataFrame(
        {"Holding Percent": [0.65, 0.10], "Name": ["Tesla Inc", "Cash"]},
        index=["TSLA", "USD"]
    )
    res1 = _classify_via_holdings("TEST1", mock_t1, {"shortName": "Test Single Stock ETF"})
    assert res1 is not None
    assert res1["type"] == "SINGLE_STOCK"
    assert res1["underlying"] == "TSLA"

    # 2. 1위 비중 >= 8% ➔ THEMATIC with anchors
    mock_t2 = MagicMock()
    mock_t2.funds_data.top_holdings = pd.DataFrame(
        {"Holding Percent": [0.15, 0.12, 0.09, 0.04], "Name": ["Nvidia", "Broadcom", "AMD", "Intel"]},
        index=["NVDA", "AVGO", "AMD", "INTC"]
    )
    res2 = _classify_via_holdings("TEST2", mock_t2, {"category": "Semiconductors"})
    assert res2 is not None
    assert res2["type"] == "THEMATIC"
    assert res2["anchors"] == ["NVDA", "AVGO", "AMD"]

    # 3. 1위 비중 < 8% ➔ BROAD_INDEX
    mock_t3 = MagicMock()
    mock_t3.funds_data.top_holdings = pd.DataFrame(
        {"Holding Percent": [0.06, 0.05, 0.04], "Name": ["A", "B", "C"]},
        index=["AAPL", "MSFT", "AMZN"]
    )
    res3 = _classify_via_holdings("TEST3", mock_t3, {})
    assert res3 is not None
    assert res3["type"] == "BROAD_INDEX"
    assert res3["macro_driven"] is True


def test_classify_via_gemini_safe_defensive_parsing(monkeypatch):
    from unittest.mock import AsyncMock
    from core.news.etf_mapper import _classify_via_gemini_safe, _ETFClassificationRawSchema

    # Gemini가 대소문자 혼합, 비정상 단어 포함, anchors에 $ 기호를 섞어서 반환한 경우 검증
    mock_raw = _ETFClassificationRawSchema(
        is_etf=True,
        type="thematic_leveraged",
        underlying=None,
        anchors=["$NVDA", "Chips", " AVGO ", "Semiconductors", "amd"],
        theme="반도체 레버리지"
    )

    class MockProvider:
        async def _generate(self, prompt, schema, temperature=0.1):
            return mock_raw

    monkeypatch.setattr("core.llm.gemini_provider.GeminiProvider", MockProvider)

    res = _classify_via_gemini_safe("SOXL_TEST", {"category": "Leveraged Tech"})
    assert res is not None
    assert res["type"] == "THEMATIC"
    # 'Chips'와 'Semiconductors' 단어는 필터링되고, 실제 주식 티커만 추출되어야 함
    assert res["anchors"] == ["NVDA", "AVGO", "AMD"]
    assert res["theme"] == "반도체 레버리지"


