import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from core import db, meritz_overseas, reconciliation
from core.config import settings
from core.engine import TradingEngine
from core.risk import RiskManager


def test_meritz_engine_initialization():
    with patch.object(settings, "BROKER", "meritz"):
        engine = TradingEngine()
        assert engine.is_meritz is True
        from core.meritz_client import AsyncMeritzClient
        assert isinstance(engine.client, AsyncMeritzClient)


def test_meritz_engine_shadow_cycle():
    async def run():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(db.SCHEMA)
        await db._migrate_add_missing_columns(conn)

        with patch.object(settings, "BROKER", "meritz"), \
             patch.object(settings, "SHADOW_MODE", True), \
             patch.object(settings, "DOMESTIC_ONLY", False), \
             patch.object(settings, "MAX_ORDER_NOTIONAL_USD", 50000.0), \
             patch.object(settings, "ENABLE_WEBSOCKET", False):

            engine = TradingEngine()
            engine.conn = conn
            engine.risk = RiskManager(conn)

            # 포트폴리오에 해외 종목(AAPL, 100%) 등록
            pid = await db.propose_target_weight(conn, "AAPL", 1.0, "user", "테스트")
            await db.decide_target_weight(conn, pid, True)
            await db.add_portfolio_symbol(conn, "AAPL", market="overseas", exchange="NASD")

            # 메리츠 해외 잔고 및 시세 모킹
            mock_present = {
                "holdings": [],
                "account_summary": [],
                "totals": {
                    "tot_asst_amt": "14000000.0",  # 1400만원 (10,000 USD @ 1400)
                    "bass_exrt": "1400.0",
                },
            }
            mock_price = {"last": "200.0", "name": "Apple", "currency": "USD"}
            mock_cash = {
                "exrt": "1400.0",
                "ovrs_ord_psbl_amt": "10000.0",
                "max_ord_psbl_qty": "50",
            }
            mock_chart = [
                {"xymd": "20261105", "open": "195", "high": "205", "low": "190", "clos": "200", "tvol": "10000"}
            ]

            with patch("core.meritz_overseas.get_present_balance_krw", new=AsyncMock(return_value=mock_present)), \
                 patch("core.meritz_overseas.get_price", new=AsyncMock(return_value=mock_price)), \
                 patch("core.meritz_overseas.get_buyable_cash", new=AsyncMock(return_value=mock_cash)), \
                 patch("core.meritz_overseas.get_daily_chart", new=AsyncMock(return_value=mock_chart)), \
                 patch("core.intraday.get_intraday_signal_overseas", new=AsyncMock(return_value=None)), \
                 patch("core.engine._is_us_market_open_now", return_value=True), \
                 patch("core.engine._is_krx_open_now", return_value=True):

                await engine._run_one_cycle()

                # 주문 intent 확인
                cur = await conn.execute("SELECT * FROM order_intents WHERE symbol = 'AAPL'")
                intent = await cur.fetchone()
                assert intent is not None
                assert intent["symbol"] == "AAPL"
                assert intent["side"] == "buy"
                assert intent["status"] == "FILLED"  # SHADOW 모드는 즉시 가상체결 처리
                assert str(intent["kis_order_no"]).startswith("SHADOW_")

                # 체결 기록(fills) 확인
                cur_fills = await conn.execute("SELECT * FROM fills WHERE intent_id = ?", (intent["intent_id"],))
                fills = await cur_fills.fetchall()
                assert len(fills) == 1
                assert fills[0]["qty"] == intent["qty"]
                assert fills[0]["price"] == intent["price"]

        await conn.close()
    asyncio.run(run())


def test_reconciliation_resolves_shadow_intents():
    async def run():
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript(db.SCHEMA)
        await db._migrate_add_missing_columns(conn)

        with patch.object(settings, "BROKER", "meritz"), \
             patch.object(settings, "SHADOW_MODE", True):

            # 가상 주문번호(SHADOW_*)가 부여된 미해결 SUBMITTED intent 삽입
            intent_id = await db.create_order_intent(
                conn, 1, "NVDA", "buy", 5.0, "limit", 120.0, "테스트", market="overseas"
            )
            await db.update_order_intent(conn, intent_id, status="SUBMITTED", kis_order_no="SHADOW_99999")

            # reconciliation 실행 (min_age_sec=0)
            mock_client = AsyncMock()
            await reconciliation.reconcile_unresolved_intents(mock_client, conn, min_age_sec=0)

            # SHADOW_* 주문은 외부 API 호출 없이 FILLED 처리되고 fills에 가상 체결이 기록되어야 함
            cur = await conn.execute("SELECT status FROM order_intents WHERE intent_id = ?", (intent_id,))
            row = await cur.fetchone()
            assert row["status"] == "FILLED"

            cur_fills = await conn.execute("SELECT * FROM fills WHERE intent_id = ?", (intent_id,))
            fills = await cur_fills.fetchall()
            assert len(fills) == 1
            assert fills[0]["qty"] == 5.0
            assert fills[0]["price"] == 120.0

        await conn.close()
    asyncio.run(run())
