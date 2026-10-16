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
from core.us_market.signals import evaluate_us_exit_guard, compute_us_signals, extract_indicator_snapshot
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

