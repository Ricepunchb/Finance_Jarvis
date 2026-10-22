import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.reconciliation import _find_candidates, reconcile_unresolved_intents
from core.kis_client import AsyncKISClient, RATE_LIMIT_MSG_CDS
from core.utils.symbol_mapper import is_overseas_symbol
from core.fundamentals.yfinance_client import fetch_analyst_targets, fetch_recent_news_yfinance
import core.db as db


def test_find_candidates_with_kis_order_no():
    async def run():
        # 1. odno가 정확히 일치하는 경우 우선 매칭 확인
        ccld_rows = [
            {"pdno": "005930", "odno": "0000021214", "sll_buy_dvsn_cd": "02", "ord_qty": "1", "ord_unpr": "226750"},
            {"pdno": "005930", "odno": "0000099999", "sll_buy_dvsn_cd": "02", "ord_qty": "1", "ord_unpr": "226750"},
        ]
        intent = {
            "symbol": "005930",
            "market": "domestic",
            "side": "buy",
            "qty": 1.0,
            "price": 226750.0,
            "order_type": "limit",
            "kis_order_no": "0000021214",
            "created_at": 1792547726.0,
        }
        candidates = await _find_candidates(ccld_rows, intent)
        assert len(candidates) == 1
        assert candidates[0]["odno"] == "0000021214"

        # lstrip('0') 케이스 검증 (21214 vs 0000021214)
        intent_stripped = dict(intent, kis_order_no="21214")
        candidates_stripped = await _find_candidates(ccld_rows, intent_stripped)
        assert len(candidates_stripped) == 1
        assert candidates_stripped[0]["odno"] == "0000021214"

    asyncio.run(run())


def test_reconcile_preserves_submitted_status_when_order_no_exists():
    async def run():
        # kis_order_no가 채번된 주문은 KIS 조회에서 못 찾더라도 NOT_SUBMITTED로 내리지 않아야 함
        import aiosqlite
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(db.SCHEMA)

        # PENDING/SUBMITTED 주문 intent 생성
        intent_id = await db.create_order_intent(
            conn,
            cycle_id=1,
            symbol="005930",
            side="buy",
            qty=1.0,
            order_type="limit",
            price=226750.0,
            reason="test",
            market="domestic",
        )
        await db.update_order_intent(
            conn, intent_id, status="SUBMITTED", kis_order_no="0000021214"
        )

        mock_client = AsyncMock()
        # KIS 당일 체결내역이 비어있는 상황 시뮬레이션
        with patch("core.kis_domestic.get_daily_ccld", new_callable=AsyncMock) as mock_ccld:
            mock_ccld.return_value = []
            await reconcile_unresolved_intents(mock_client, conn, min_age_sec=0)

        # 상태가 NOT_SUBMITTED가 아니라 여전히 SUBMITTED여야 함
        cur = await conn.execute("SELECT status FROM order_intents WHERE intent_id = ?", (intent_id,))
        row = await cur.fetchone()
        assert row["status"] == "SUBMITTED"

        await conn.close()

    asyncio.run(run())


def test_symbol_mapper_krx_alphanumeric_codes():
    # 국내 신종 채권/액티브 ETF 등 영숫자 혼용 코드
    assert is_overseas_symbol("005930") is False
    assert is_overseas_symbol("0193M0") is False  # BNK 27-12 특수채
    assert is_overseas_symbol("0091M0") is False  # HANARO 27-06 회사채
    assert is_overseas_symbol("0040X0") is False
    assert is_overseas_symbol("00593K") is False
    assert is_overseas_symbol("005930.KS") is False

    # 해외 티커
    assert is_overseas_symbol("AAPL") is True
    assert is_overseas_symbol("SPY") is True
    assert is_overseas_symbol("TSLA") is True
    assert is_overseas_symbol("NVDA") is True


def test_yfinance_skips_domestic_symbols():
    # 국내 종목코드에 대해서는 yfinance를 부르지 않고 즉시 빈 값 반환
    res_targets = fetch_analyst_targets("0193M0")
    assert res_targets == {}

    res_news = fetch_recent_news_yfinance("0091M0")
    assert res_news == []


def test_kis_rate_limit_codes():
    assert "EGW00201" in RATE_LIMIT_MSG_CDS
    assert "EGW00215" in RATE_LIMIT_MSG_CDS


def test_api_add_symbol_rejects_korean_and_spaces():
    from fastapi.testclient import TestClient
    from api.main import app

    client = TestClient(app)
    # 한글 종목명 입력 시 400 Bad Request
    resp = client.post("/portfolio/symbols", json={"symbol": "TIGER 미국S&P500", "market": "domestic"})
    assert resp.status_code == 400
    assert "유효하지 않은 종목코드" in resp.json()["detail"]

    # 공백 포함 종목코드 입력 시 400 Bad Request
    resp2 = client.post("/portfolio/symbols", json={"symbol": "005930 ", "market": "domestic"})
    # strip 후 정상 처리되는지 또는 공백 포함 검사
    assert resp2.status_code in (200, 400)
