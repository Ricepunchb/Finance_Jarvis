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
    await db._migrate_add_missing_columns(conn)
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


def test_rebalance_periodic_and_discovery_config_defaults():
    assert settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS == 7
    assert settings.AI_REBALANCE_DISCOVERY_TRIGGER_SCORE == 0.45
    assert settings.DISCOVERY_MIN_SCORE == 0.40


def test_periodic_due_respects_7_day_interval():
    async def run():
        conn = await _conn()
        scheduler = PortfolioScheduler(AsyncMock())

        # 최초 실행 시에는 즉시 due=True
        assert await scheduler._periodic_due(conn) is True

        # 5일 경과 시에는 아직 7일이 안 되었으므로 due=False
        five_days_ago = time.time() - (5 * 86400)
        await db.set_state(conn, "ai_rebalance_last_scheduled_run_at", str(five_days_ago))
        assert await scheduler._periodic_due(conn) is False

        # 7.1일 경과 시에는 due=True
        seven_days_ago = time.time() - (7.1 * 86400)
        await db.set_state(conn, "ai_rebalance_last_scheduled_run_at", str(seven_days_ago))
        assert await scheduler._periodic_due(conn) is True

        await conn.close()

    asyncio.run(run())


def test_discovery_trigger_detail_and_deduplication():
    import json
    from core import discovery

    async def run():
        conn = await _conn()
        scheduler = PortfolioScheduler(AsyncMock())

        # 기존 보유 종목: 005930 (삼성전자)
        await db.add_portfolio_symbol(conn, "005930", market="domestic")
        pid = await db.propose_target_weight(conn, "005930", 0.5, "manual")
        await db.decide_target_weight(conn, pid, True)

        # 후보 유니버스 등록: 005930(보유), 014830(유니드, 고득점), 064350(현대로템, 중득점), 000270(기아, 고득점)
        await db.add_candidate_symbol(conn, "005930", "삼성전자", "SEED")
        await db.add_candidate_symbol(conn, "014830", "유니드", "DYNAMIC")
        await db.add_candidate_symbol(conn, "064350", "현대로템", "DYNAMIC")
        await db.add_candidate_symbol(conn, "000270", "기아", "DYNAMIC")

        now = time.time()
        scored_entries = [
            {"symbol": "005930", "name": "삼성전자", "score": 0.60, "angle": "momentum", "angle_label": "모멘텀"},
            {"symbol": "014830", "name": "유니드", "score": 0.48, "angle": "value", "angle_label": "저평가"},
            {"symbol": "064350", "name": "현대로템", "score": 0.42, "angle": "value", "angle_label": "저평가"},
        ]
        await db.set_state(
            conn,
            discovery.STATE_SCORED_CACHE,
            json.dumps({"scored_at": now, "entries": scored_entries}, ensure_ascii=False),
        )

        # 1) 첫 검사: 보유 중인 005930(0.60)과 임계치(0.45) 미만인 064350(0.42)은 제외되고 014830(0.48)이 포착되어야 함
        detail1 = await scheduler._discovery_trigger_detail(conn)
        assert detail1 is not None
        assert "014830" in detail1
        assert "유니드" in detail1
        assert "0.48" in detail1

        # 2) 동일 캐시로 즉시 재검사: 014830은 이미 트리거 기록에 있으므로 중복 발동하지 않아야 함
        detail2 = await scheduler._discovery_trigger_detail(conn)
        assert detail2 is None

        # 3) 새로운 고득점 후보 000270(기아, 0.52)이 캐시에 추가되면 즉시 다시 포착되어야 함
        scored_entries.append(
            {"symbol": "000270", "name": "기아", "score": 0.52, "angle": "value", "angle_label": "저평가"}
        )
        await db.set_state(
            conn,
            discovery.STATE_SCORED_CACHE,
            json.dumps({"scored_at": now + 10, "entries": scored_entries}, ensure_ascii=False),
        )
        detail3 = await scheduler._discovery_trigger_detail(conn)
        assert detail3 is not None
        assert "000270" in detail3
        assert "기아" in detail3

        await conn.close()

    asyncio.run(run())


def test_scheduler_tick_executes_discovery_trigger_between_periodic_runs(tmp_path, monkeypatch):
    import json
    from core import discovery

    db_file = tmp_path / "test_sched_disc.db"
    monkeypatch.setattr(settings, "DB_PATH", str(db_file))

    async def run():
        await db.init_db()
        conn = await db.get_connection()
        try:
            await db.set_state(conn, "ai_rebalance_scheduler_enabled", "1")
            await db.set_state(conn, "ai_rebalance_auto_apply", "1")
            # 정기 실행은 2일 전에 이미 수행됨 (7일 주기 내이므로 정기 트리거는 안 켜짐)
            await db.set_state(conn, "ai_rebalance_last_scheduled_run_at", str(time.time() - 2 * 86400))

            await db.add_portfolio_symbol(conn, "005930", market="domestic")
            pid = await db.propose_target_weight(conn, "005930", 0.5, "manual")
            await db.decide_target_weight(conn, pid, True)

            await db.add_candidate_symbol(conn, "014830", "유니드", "DYNAMIC")
            await db.set_state(
                conn,
                discovery.STATE_SCORED_CACHE,
                json.dumps(
                    {
                        "scored_at": time.time(),
                        "entries": [
                            {"symbol": "014830", "name": "유니드", "score": 0.51, "angle": "value", "angle_label": "저평가"}
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
        finally:
            await conn.close()

        scheduler = PortfolioScheduler(AsyncMock())
        with patch.object(scheduler, "_maybe_refresh_discovery", new_callable=AsyncMock, return_value=False), \
             patch("core.portfolio_agent.propose_rebalance", new_callable=AsyncMock) as mock_propose, \
             patch("core.db.apply_rebalance_event", new_callable=AsyncMock) as mock_apply:
            mock_propose.return_value = {"event_id": 77, "status": "PROPOSED"}

            await scheduler._tick()

            assert mock_propose.call_count == 1
            _, kwargs = mock_propose.call_args
            assert kwargs["trigger_type"] == "DISCOVERY"
            assert "014830" in kwargs["trigger_detail"]
            assert mock_apply.call_count == 1
            assert mock_apply.call_args[0][1] == 77
            assert mock_apply.call_args[1]["approve"] is True

    asyncio.run(run())


