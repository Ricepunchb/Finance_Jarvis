# tests/test_us_market.py
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np
from fastapi.testclient import TestClient

from api.main import app
from core import db
from core.us_market.client import get_us_market_status
from core.us_market.signals import (
    evaluate_us_exit_guard,
    compute_us_signals,
    extract_indicator_snapshot,
    check_buy_confirmation,
)
from core.us_market.notifier import format_signal_alert_message


def test_us_market_status():
    """미국장 운영 상태 판정 테스트."""
    status = get_us_market_status()
    assert "status" in status
    assert "status_label" in status
    assert "ny_time_str" in status
    assert "kst_time_str" in status
    assert "is_regular_open" in status
    assert status["status"] in ("REGULAR", "PRE_MARKET", "AFTER_HOURS", "CLOSED")


def test_us_exit_guard_stop_loss():
    """손절 기준 도달 시 STOP_LOSS 트리거 발동 검증."""
    # 평단가 100달러, 현재가 90달러 (-10% 하락)
    res = evaluate_us_exit_guard(current_price=90.0, avg_entry_price=100.0, peak_price=100.0)
    assert res is not None
    assert res["trigger"] == "STOP_LOSS"
    assert "손절 경고" in res["reason"]


def test_us_exit_guard_trailing_take_profit():
    """진입 후 고점(+10%) 달성 후 고점 대비 하락 시 트레일링 익절 발동 검증."""
    # 평단가 100달러, 고점 120달러 도달 후 현재가 110달러 (고점 대비 -8.3% 하락)
    res = evaluate_us_exit_guard(current_price=110.0, avg_entry_price=100.0, peak_price=120.0)
    assert res is not None
    assert res["trigger"] == "TRAILING_TAKE_PROFIT"
    assert "트레일링 익절" in res["reason"]


def test_us_exit_guard_normal_holding():
    """정상 범위(손절선 미달, 익절 하락 없음)에서는 트리거가 발생하지 않음 검증."""
    res = evaluate_us_exit_guard(current_price=102.0, avg_entry_price=100.0, peak_price=103.0)
    assert res is None


def test_us_exit_guard_dynamic_atr_stop():
    """3번 안: 변동성(ATR) 기반 동적 손절선 동작 검증.
    고변동성 종목(ATR 7%)의 경우 기본 -6%~-8%에 털리지 않고 14%까지 여유를 두는지 검증.
    """
    # 1. ATR 7% 종목에서 -8% 일중 하락 발생: 기존 고정 손절선(-6%)이면 손절되지만, 동적 손절(-14%)에서는 생존
    res1 = evaluate_us_exit_guard(
        current_price=92.0,
        avg_entry_price=100.0,
        peak_price=100.0,
        atr_pct=0.07,
    )
    assert res1 is None  # 손절되지 않고 홀딩 유지

    # 2. 동적 손절 한계치(-14%)를 초과하는 -15% 하락 발생: 손절 트리거 발동
    res2 = evaluate_us_exit_guard(
        current_price=85.0,
        avg_entry_price=100.0,
        peak_price=100.0,
        atr_pct=0.07,
    )
    assert res2 is not None
    assert res2["trigger"] == "STOP_LOSS"
    assert "변동성 적응 손절 기준 -14.0%" in res2["reason"]


def _make_dummy_ohlcv(n_bars=70, base_price=100.0, trend=0.3):
    dates = pd.date_range("2026-01-01", periods=n_bars, freq="30min")
    prices = [base_price + i * trend + np.sin(i / 3.0) * 1.5 for i in range(n_bars)]
    return pd.DataFrame({
        "date": dates,
        "open": prices,
        "high": [p + 1.0 for p in prices],
        "low": [p - 1.0 for p in prices],
        "close": prices,
        "volume": [1000.0] * n_bars,
    })


def test_compute_us_signals_uptrend():
    """상승 추세 데이터에서 BUY/HOLD 시그널 및 권고문 산출 검증."""
    df_30m = _make_dummy_ohlcv(70, 100.0, trend=0.3)
    df_daily = _make_dummy_ohlcv(70, 100.0, trend=0.3)

    sig = compute_us_signals(
        symbol="AAPL",
        df_30m=df_30m,
        df_daily=df_daily,
        current_price=150.0,
        holding={"avg_entry_price": 140.0, "held_qty": 10.0, "peak_price": 150.0},
        consensus={"target_gap_pct": 25.0, "recommendation_key": "buy"},
    )
    assert sig["symbol"] == "AAPL"
    assert sig["combined_action"] in ("BUY", "HOLD", "SELL")
    assert "action_recommendation" in sig
    assert sig["exit_trigger"] == "NONE"


def test_format_signal_alert_message():
    """텔레그램 메시지 서식 생성 검증."""
    sig_data = {
        "symbol": "NVDA",
        "price": 128.50,
        "combined_action": "BUY",
        "strength": 0.85,
        "exit_trigger": "NONE",
        "action_recommendation": "🟢 [적극 매수] 30분봉 눌림목 반등 및 일봉 상승 추세 지속",
        "pnl_pct": 7.5,
    }
    msg = format_signal_alert_message(sig_data, market_status_str="정규장")
    assert "NVDA" in msg
    assert "128.50" in msg
    assert "BUY" in msg
    assert "적극 매수" in msg


def test_us_market_api_endpoints():
    """FastAPI 미국장 엔드포인트 통합 테스트."""
    client = TestClient(app)

    # 1. 상태 조회
    r1 = client.get("/us-market/status")
    assert r1.status_code == 200
    assert "status" in r1.json()

    # 2. Watchlist 등록
    r2 = client.post("/us-market/watchlist", json={
        "symbol": "TESTSYM",
        "name": "Test Company",
        "sector": "Tech",
        "avg_entry_price": 50.0,
        "held_qty": 2.0,
    })
    assert r2.status_code == 200
    assert r2.json()["symbol"] == "TESTSYM"

    # 3. Watchlist 목록 조회
    r3 = client.get("/us-market/watchlist")
    assert r3.status_code == 200
    symbols = [it["symbol"] for it in r3.json()]
    assert "TESTSYM" in symbols

    # 4. Watchlist 삭제
    r4 = client.delete("/us-market/watchlist/TESTSYM")
    assert r4.status_code == 200
    assert r4.json()["status"] == "deleted"


def test_notify_signal_only_on_change():
    """동일 시그널 반복 발송 방지 및 시그널 전환 시에만 발송되는지 검증."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from core.us_market.notifier import notify_signal_if_needed, _LAST_ALERT_SENT

    async def _run():
        _LAST_ALERT_SENT.clear()

        buy_sig = {
            "symbol": "DEDUP_TEST",
            "price": 100.0,
            "combined_action": "BUY",
            "exit_trigger": "NONE",
            "strength": 0.8,
            "action_recommendation": "매수 적기",
        }
        stop_sig = {
            "symbol": "DEDUP_TEST",
            "price": 90.0,
            "combined_action": "SELL",
            "exit_trigger": "STOP_LOSS",
            "strength": 0.9,
            "action_recommendation": "손절 경고",
        }

        weak_buy_sig = {
            "symbol": "DEDUP_TEST",
            "price": 100.0,
            "combined_action": "BUY",
            "exit_trigger": "NONE",
            "strength": 0.45,  # 45% (< 60% 임계치)
            "action_recommendation": "매수 관심",
        }

        with patch("core.us_market.notifier.send_telegram_message_async", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = (True, "성공")

            # 0. 60% 미만 약한 시그널(45%) -> 발송되지 않아야 함!
            res0 = await notify_signal_if_needed(
                weak_buy_sig, token="dummy", chat_id="123", last_alert_key="HOLD:NONE"
            )
            assert res0 is False
            assert mock_send.call_count == 0

            # 1. 60% 이상 강한 시그널(80%)로 BUY 변경 -> 발송되어야 함!
            res1 = await notify_signal_if_needed(
                buy_sig, token="dummy", chat_id="123", last_alert_key="HOLD:NONE"
            )
            assert res1 is True
            assert mock_send.call_count == 1

            # 2. 다음 주기에서도 여전히 BUY 상태 (시그널 변경 없음) -> 발송되지 않아야 함!
            res2 = await notify_signal_if_needed(
                buy_sig, token="dummy", chat_id="123", last_alert_key="BUY:NONE"
            )
            assert res2 is False
            assert mock_send.call_count == 1  # 발송 횟수 증가 없음!

            # 3. 손절(STOP_LOSS)로 시그널 전환 -> 강도와 무관하게 즉시 발송되어야 함!
            res3 = await notify_signal_if_needed(
                stop_sig, token="dummy", chat_id="123", last_alert_key="BUY:NONE"
            )
            assert res3 is True
            assert mock_send.call_count == 2

    asyncio.run(_run())


def test_us_watchlist_last_alert_key_persistence():
    """DB의 us_watchlist에서 last_alert_key가 올바르게 조회 및 영속화되는지 검증."""
    import asyncio
    import aiosqlite
    from core import db

    async def _run():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(db.SCHEMA)

        # 1. 종목 등록
        await db.upsert_us_watchlist_item(conn, "AAPL", name="Apple Inc")
        item = await db.get_us_watchlist_item(conn, "AAPL")
        assert item is not None
        assert "last_alert_key" in item
        assert item["last_alert_key"] is None

        # 2. 알림 상태 업데이트 (BUY:NONE)
        await db.update_us_watchlist_alert_state(conn, "AAPL", "BUY:NONE")

        # 3. get_us_watchlist_item 조회 시 last_alert_key가 정확히 로드되는지 확인
        item_updated = await db.get_us_watchlist_item(conn, "AAPL")
        assert item_updated is not None
        assert item_updated["last_alert_key"] == "BUY:NONE"
        assert item_updated["last_alert_ts"] is not None

        # 4. list_us_watchlist 조회 시에도 last_alert_key가 포함되는지 확인
        all_items = await db.list_us_watchlist(conn)
        assert len(all_items) == 1
        assert all_items[0]["symbol"] == "AAPL"
        assert all_items[0]["last_alert_key"] == "BUY:NONE"

        await conn.close()

    asyncio.run(_run())


def test_check_buy_confirmation_filters():
    """4번 안: 30분봉 음봉 하락 및 일봉 역배열 시 매수 확증 차단 테스트."""
    dates = pd.date_range("2026-01-01", periods=5, freq="30min")

    # 1. 30분봉 음봉 하락 진행 중인 경우 차단
    df_bearish_bar = pd.DataFrame({
        "date": dates,
        "open": [100.0, 101.0, 102.0, 103.0, 104.0],
        "high": [101.0, 102.0, 103.0, 104.0, 104.5],
        "low": [99.0, 100.0, 101.0, 102.0, 100.0],
        "close": [100.5, 101.5, 102.5, 103.5, 101.0],  # open 104.0 -> close 101.0 (장대 음봉)
        "volume": [1000.0] * 5,
    })
    confirmed1, reason1 = check_buy_confirmation(
        df_30m=df_bearish_bar,
        df_daily=pd.DataFrame(),
        intraday_snap={},
        daily_snap={},
    )
    assert confirmed1 is False
    assert "음봉 하락" in reason1

    # 2. 일봉 하락 추세(close < sma20) 중 30분봉 SMA20 하회 및 MACD 음수 차단
    df_bullish_bar = pd.DataFrame({
        "date": dates,
        "open": [100.0, 100.5, 101.0, 101.5, 102.0],
        "high": [101.0, 101.5, 102.0, 102.5, 103.5],
        "low": [99.5, 100.0, 100.5, 101.0, 101.8],
        "close": [100.5, 101.0, 101.5, 102.0, 103.0],  # 양봉
        "volume": [1000.0] * 5,
    })
    confirmed2, reason2 = check_buy_confirmation(
        df_30m=df_bullish_bar,
        df_daily=pd.DataFrame(),
        intraday_snap={"sma20": 105.0, "macd_hist": -0.5},
        daily_snap={"close": 103.0, "sma20": 110.0},
    )
    assert confirmed2 is False
    assert "일봉 하락 추세 중 30분봉 SMA20 하회" in reason2

    # 3. 정상 확증 통과 (양봉 + 지지 확인)
    confirmed3, reason3 = check_buy_confirmation(
        df_30m=df_bullish_bar,
        df_daily=pd.DataFrame(),
        intraday_snap={"sma20": 101.0, "macd_hist": 0.2},
        daily_snap={"close": 115.0, "sma20": 110.0},
    )
    assert confirmed3 is True


def test_compute_us_signals_consensus_bias_and_candle_confirmation():
    """SPCX 시나리오 검증: 일봉 하락 추세 + 컨센서스 괴리율 +70%일 때
    맹목적 BUY 왜곡을 억제하고 HOLD(관망/지지대기)로 휩소를 차단하는지 검증.
    """
    # 70개 봉의 하락 추세 데이터 생성 (일봉 및 30분봉 모두 하향)
    df_30m = _make_dummy_ohlcv(70, 150.0, trend=-0.5)
    df_daily = _make_dummy_ohlcv(70, 150.0, trend=-0.5)

    sig = compute_us_signals(
        symbol="SPCX",
        df_30m=df_30m,
        df_daily=df_daily,
        current_price=110.0,
        holding=None,
        consensus={"target_gap_pct": 77.0, "recommendation_key": "strong_buy"},
    )
    # 1. 일봉 하락 추세이므로 목표가 괴리율 +77%에 의한 맹목적 BUY가 HOLD로 억제됨
    assert sig["valuation_signal"]["direction"] == "HOLD"
    # 2. 결과적으로 시그널이 섣부른 적극매수로 왜곡되지 않고 HOLD 또는 SELL로 방어됨
    assert sig["combined_action"] in ("HOLD", "SELL")
    assert sig["combined_action"] != "BUY"


def test_us_consensus_and_news_proxy_mapping(monkeypatch):
    """TSLW 등 단일 기초자산 ETF가 기초자산(TSLA)의 컨센서스와 뉴스를 프록시 상속하는지 검증."""
    from unittest.mock import MagicMock
    from core.us_market.client import _fetch_consensus_and_news_sync

    queried_symbols = []

    class MockTicker:
        def __init__(self, sym):
            queried_symbols.append(sym)
            self.info = {
                "targetMeanPrice": 420.0,
                "currentPrice": 350.0,
                "recommendationKey": "buy",
                "recommendationMean": 2.0,
            }
            self.news = [
                {
                    "title": "Tesla Announces Record Deliveries",
                    "link": "https://example.com/tsla-news",
                    "publisher": "Reuters",
                    "providerPublishTime": 1793010000,
                }
            ]

    monkeypatch.setattr("yfinance.Ticker", MockTicker)

    res = _fetch_consensus_and_news_sync("TSLW")
    assert "TSLA" in queried_symbols
    assert res["symbol"] == "TSLW"
    assert res["underlying"] == "TSLA"
    assert res["is_etf_proxy"] is True
    assert res["target_mean"] == 420.0
    assert res["target_gap_pct"] == 20.0
    assert len(res["news"]) == 1
    assert "Tesla Announces" in res["news"][0]["title"]


def test_strip_all_emojis_clean():
    """이모지 제거 함수가 한글을 보존하면서 유니코드 이모지만 100% 제거하는지 검증."""
    from core.us_market.surprise import strip_all_emojis, EMOJI_PATTERN

    sample_text = "🚨 [글로벌 뉴스 서프라이즈] 📈 호재! NVDA 엔비디아 실적 ⏰ 발표 🚀 반도체 ⭐ 100점 ⚡"
    cleaned = strip_all_emojis(sample_text)

    # 1. 한글 및 텍스트 보존 확인
    assert "글로벌 뉴스 서프라이즈" in cleaned
    assert "호재!" in cleaned
    assert "NVDA 엔비디아 실적" in cleaned
    assert "발표" in cleaned
    assert "반도체" in cleaned
    assert "100점" in cleaned

    # 2. 이모지가 완전히 제거되었는지 패턴 매칭 검증
    assert EMOJI_PATTERN.search(cleaned) is None


def test_cluster_news_articles():
    """동일 사건에 대한 다수 언론사 기사가 1개의 클러스터로 자동 묶이는지 검증."""
    import time
    from core.us_market.surprise import NewsArticle, cluster_news_articles

    now = time.time()
    articles = [
        NewsArticle(
            symbol="NVDA",
            title="Exclusive: Nvidia AI chips face delays in Hong Kong amid China import approval review",
            summary="China authorities are reviewing import licenses.",
            source="Reuters",
            published_at=now - 300,
        ),
        NewsArticle(
            symbol="NVDA",
            title="Nvidia chips pile up in Hong Kong as China holds import approvals",
            summary="Supply chain bottleneck in Hong Kong.",
            source="Bloomberg",
            published_at=now - 500,
        ),
        NewsArticle(
            symbol="NVDA",
            title="Why Warren Buffett is buying dividend stocks right now",
            summary="General market opinion.",
            source="Motley Fool",
            published_at=now - 1000,
        ),
        NewsArticle(
            symbol="AAPL",
            title="Apple unveils new M4 chip with AI capabilities",
            summary="Hardware announcement.",
            source="CNBC",
            published_at=now - 100,
        ),
    ]

    clusters = cluster_news_articles(articles, similarity_threshold=0.35, time_window_hours=6.0)

    # 4개의 기사 중 NVDA의 홍콩 칩 기사 2건은 1개로 묶여 총 3개의 클러스터가 생성되어야 함
    assert len(clusters) == 3

    # NVDA 첫 번째 클러스터 검증
    nvda_cluster = next(c for c in clusters if c.symbol == "NVDA" and c.count == 2)
    assert nvda_cluster.count == 2
    assert "Reuters" in nvda_cluster.sources
    assert "Bloomberg" in nvda_cluster.sources

    # AAPL 클러스터 분리 검증
    aapl_cluster = next(c for c in clusters if c.symbol == "AAPL")
    assert aapl_cluster.count == 1
    assert aapl_cluster.sources == ["CNBC"]


def test_format_news_surprise_telegram_message():
    """텔레그램 메시지 서식이 이모지 없이 최대 3문장 요약, 호재/악재, 섹터, Surprise 점수를 포함하는지 검증."""
    from core.us_market.surprise import (
        EMOJI_PATTERN,
        SurpriseAnalysisResult,
        format_news_surprise_telegram_message,
    )

    analysis = SurpriseAnalysisResult(
        surprise_score=85,
        impact_type="호재",
        affected_sector_theme="AI 반도체 및 고성능 컴퓨팅",
        summary_sentences=[
            "1분기 매출이 컨센서스를 15% 상회하며 분기 사상 최대치를 기록했습니다.",
            "차세대 블랙웰 칩의 연간 선주문량이 이미 전량 완판되었습니다.",
            "클라우드 하이퍼스케일러들의 AI 인프라 투자 지속으로 견조한 수요가 예상됩니다.",
            "네 번째 문장은 3문장 제한 규칙에 의해 자동 탈락되어야 합니다.",
        ],
        rationale="컨센서스 대비 어닝 서프라이즈 및 가이던스 상향",
    )

    msg = format_news_surprise_telegram_message(
        symbol="NVDA",
        company_name="NVIDIA Corporation",
        analysis=analysis,
        sources=["Reuters", "Bloomberg"],
        cluster_count=2,
    )

    # 1. 이모지 완전 배제 확인
    assert EMOJI_PATTERN.search(msg) is None

    # 2. 필수 요건 필드 확인
    assert "[글로벌 뉴스 서프라이즈]" in msg
    assert "대상 종목: NVDA (NVIDIA Corporation)" in msg
    assert "Surprise 점수: 85점" in msg
    assert "시장 영향: 호재" in msg
    assert "영향 섹터/테마: AI 반도체 및 고성능 컴퓨팅" in msg
    assert "1. 1분기 매출이 컨센서스를 15% 상회하며" in msg
    assert "2. 차세대 블랙웰 칩의 연간 선주문량이" in msg
    assert "3. 클라우드 하이퍼스케일러들의 AI 인프라" in msg
    # 최대 3문장 초과분은 포함되지 않아야 함
    assert "네 번째 문장" not in msg
    assert "Reuters, Bloomberg (외 1개 매체)" in msg


def test_db_news_alerts_and_deduplication():
    """뉴스 서프라이즈 알림 DB 기록 및 동일 클러스터 24시간 중복 방지 검증."""
    import asyncio
    import aiosqlite
    from core import db

    async def _run():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(db.SCHEMA)

        cluster_id = "test_cluster_nvda_001"

        # 1. 발송 전: is_us_news_cluster_already_sent는 False여야 함
        is_sent = await db.is_us_news_cluster_already_sent(conn, cluster_id, max_age_hours=24.0)
        assert is_sent is False

        # 2. 알림 기록
        row_id = await db.record_us_news_alert(
            conn=conn,
            cluster_id=cluster_id,
            symbol="NVDA",
            title="Nvidia Q3 Earnings Beat Expectations",
            surprise_score=88,
            impact_type="호재",
            affected_sector="반도체",
            summary="1. 어닝 서프라이즈 달성",
            source="Reuters",
            cluster_count=2,
        )
        assert row_id > 0

        # 3. 발송 후: 동일 클러스터 ID는 True로 반환되어야 함
        is_sent_now = await db.is_us_news_cluster_already_sent(conn, cluster_id, max_age_hours=24.0)
        assert is_sent_now is True

        # 4. 목록 조회 검증
        alerts = await db.list_us_news_alerts(conn, limit=10, symbol="NVDA")
        assert len(alerts) == 1
        assert alerts[0]["symbol"] == "NVDA"
        assert alerts[0]["surprise_score"] == 88
        assert alerts[0]["impact_type"] == "호재"
        await conn.close()

    asyncio.run(_run())


def test_notify_news_surprise_threshold(monkeypatch):
    """Surprise 점수가 임계값 미만이면 텔레그램 발송을 생략하고, 이상일 때만 발송하는지 검증."""
    import asyncio
    from core.us_market.notifier import notify_news_surprise_if_needed
    from core.us_market.surprise import SurpriseAnalysisResult

    sent_calls = []

    def mock_send(token, chat_id, text, parse_mode="", timeout=10):
        sent_calls.append({"text": text, "parse_mode": parse_mode})
        return True, "성공"

    monkeypatch.setattr("core.us_market.notifier.send_telegram_message_with_result", mock_send)

    low_analysis = SurpriseAnalysisResult(
        surprise_score=60,  # 75점 미만
        impact_type="중립",
        affected_sector_theme="거시경제",
        summary_sentences=["단순 시황 브리핑입니다."],
        rationale="기선반영 시황",
    )

    high_analysis = SurpriseAnalysisResult(
        surprise_score=82,  # 75점 이상
        impact_type="호재",
        affected_sector_theme="소프트웨어 AI",
        summary_sentences=["대형 클라우드 공급 계약 수주 성공."],
        rationale="예상치 못한 대형 수주",
    )

    async def _run():
        # 1. 60점: threshold 75 미만이므로 발송되지 않음
        ok1, _ = await notify_news_surprise_if_needed(
            symbol="PLTR",
            analysis=low_analysis,
            token="mock_token",
            chat_id="12345",
            threshold=75,
        )
        assert ok1 is False
        assert len(sent_calls) == 0

        # 2. 82점: threshold 75 이상이므로 발송됨
        ok2, _ = await notify_news_surprise_if_needed(
            symbol="PLTR",
            analysis=high_analysis,
            token="mock_token",
            chat_id="12345",
            threshold=75,
        )
        assert ok2 is True
        assert len(sent_calls) == 1
        assert "PLTR" in sent_calls[0]["text"]
        assert "Surprise 점수: 82점" in sent_calls[0]["text"]

    asyncio.run(_run())


def test_us_news_api_endpoints(monkeypatch):
    """GET /us-market/news-alerts 및 POST /us-market/scan-news API 정상 동작 검증."""
    from unittest.mock import AsyncMock

    mock_news_result = {
        "scanned_symbols": 2,
        "total_articles": 10,
        "clusters_count": 3,
        "processed_clusters": [],
        "alerted_count": 1,
        "alerted_clusters": [
            {
                "symbol": "NVDA",
                "title": "Nvidia Beats Q3 Estimates",
                "surprise_score": 88,
                "impact_type": "호재",
            }
        ],
    }

    monkeypatch.setattr(
        "core.us_market.service.scan_us_market_news",
        AsyncMock(return_value=mock_news_result),
    )

    client = TestClient(app)

    # 1. GET /us-market/news-alerts
    resp = client.get("/us-market/news-alerts?limit=10")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

    # 2. POST /us-market/scan-news
    post_resp = client.post(
        "/us-market/scan-news",
        json={"symbols": ["NVDA"], "threshold": 75, "send_notifications": False},
    )
    assert post_resp.status_code == 200
    data = post_resp.json()
    assert data["status"] == "ok"
    assert data["threshold"] == 75
    assert data["alerted_count"] == 1
    assert data["scanned_symbols"] == 2





