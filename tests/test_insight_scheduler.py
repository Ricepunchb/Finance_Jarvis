import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo
import aiosqlite
import pytest

from core import db
from core.config import settings
from core.portfolio_scheduler import PortfolioScheduler

KST = ZoneInfo("Asia/Seoul")


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    return conn


def test_insight_schedule_hours_config():
    assert settings.INSIGHT_SCHEDULE_HOURS_KST == [9, 12, 15, 21]


def test_maybe_refresh_discovery_triggers_at_scheduled_hours():
    async def run():
        conn = await _conn()
        mock_client = AsyncMock()
        scheduler = PortfolioScheduler(mock_client)

        # 1. 09시 KST: 첫 실행 시 트리거되어야 함
        dt_09 = datetime(2026, 10, 23, 9, 0, 0, tzinfo=KST)
        with patch("core.portfolio_scheduler.datetime") as mock_dt, \
             patch("core.discovery_sources.refresh_dynamic_universe", new_callable=AsyncMock) as mock_refresh:
            mock_dt.now.return_value = dt_09
            mock_dt.fromtimestamp = datetime.fromtimestamp

            await scheduler._maybe_refresh_discovery(conn)
            assert mock_refresh.call_count == 1
            assert mock_refresh.call_args[1].get("trigger_type") == "SCHEDULED"

            slot = await db.get_state(conn, "last_insight_schedule_slot")
            assert slot == "20261023_09"

            # 2. 09시 30분 KST: 같은 슬롯(09시) 내에서는 중복 실행되지 않아야 함
            dt_09_30 = datetime(2026, 10, 23, 9, 30, 0, tzinfo=KST)
            mock_dt.now.return_value = dt_09_30
            await scheduler._maybe_refresh_discovery(conn)
            assert mock_refresh.call_count == 1  # 여전히 1회 유지

            # 3. 10시 KST: 스케줄 시각(9, 12, 15, 21)이 아니므로 실행되지 않아야 함
            dt_10 = datetime(2026, 10, 23, 10, 0, 0, tzinfo=KST)
            mock_dt.now.return_value = dt_10
            await scheduler._maybe_refresh_discovery(conn)
            assert mock_refresh.call_count == 1

            # 4. 12시 KST: 새 슬롯(12시)이므로 다시 트리거되어야 함
            dt_12 = datetime(2026, 10, 23, 12, 0, 0, tzinfo=KST)
            mock_dt.now.return_value = dt_12
            await scheduler._maybe_refresh_discovery(conn)
            assert mock_refresh.call_count == 2
            slot = await db.get_state(conn, "last_insight_schedule_slot")
            assert slot == "20261023_12"

            # 5. 15시 KST: 오후 3시 트리거
            dt_15 = datetime(2026, 10, 23, 15, 0, 0, tzinfo=KST)
            mock_dt.now.return_value = dt_15
            await scheduler._maybe_refresh_discovery(conn)
            assert mock_refresh.call_count == 3
            slot = await db.get_state(conn, "last_insight_schedule_slot")
            assert slot == "20261023_15"

            # 6. 21시 KST: 오후 9시 트리거
            dt_21 = datetime(2026, 10, 23, 21, 0, 0, tzinfo=KST)
            mock_dt.now.return_value = dt_21
            await scheduler._maybe_refresh_discovery(conn)
            assert mock_refresh.call_count == 4
            slot = await db.get_state(conn, "last_insight_schedule_slot")
            assert slot == "20261023_21"

        await conn.close()

    asyncio.run(run())

