import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import aiosqlite

from core import db, discovery, portfolio_agent
from core.config import settings
from core.portfolio_scheduler import PortfolioScheduler


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    await db._migrate_add_missing_columns(conn)
    return conn


def test_recently_removed_symbols_excludes_active_and_re_added():
    async def run():
        conn = await _conn()
        for s in ("035900", "005930", "000660"):
            await db.add_portfolio_symbol(conn, s)
        await db.finish_symbol_removal(conn, "035900")
        await db.finish_symbol_removal(conn, "000660")
        assert set(await db.list_recently_removed_symbols(conn, 30)) == {"035900", "000660"}

        await db.add_portfolio_symbol(conn, "000660")  # 다시 담으면 쿨다운 대상에서 빠진다
        assert await db.list_recently_removed_symbols(conn, 30) == ["035900"]

        await conn.execute("UPDATE portfolio_symbols SET disabled_at = disabled_at - 40 * 86400 WHERE symbol = '035900'")
        assert await db.list_recently_removed_symbols(conn, 30) == []
    asyncio.run(run())


def test_removed_symbol_high_score_exemption_in_discovery_trigger():
    """청산/삭제된 종목이라도 발굴 점수 >= 0.45(상위 4~5%)이면 쿨다운이 즉시 면제되어 발굴 트리거에 참여한다."""
    async def run():
        conn = await _conn()
        scheduler = PortfolioScheduler(AsyncMock())

        # 활성 포트폴리오 1종목 구성
        await db.add_portfolio_symbol(conn, "005930")
        pid = await db.propose_target_weight(conn, "005930", 0.5, "manual")
        await db.decide_target_weight(conn, pid, True)

        # 088980 종목을 후보 유니버스에 등록 후 삭제(쿨다운 상태)
        await db.add_candidate_symbol(conn, "088980", "맥쿼리인프라", "DYNAMIC")
        await db.add_portfolio_symbol(conn, "088980")
        await db.finish_symbol_removal(conn, "088980")

        assert "088980" in await db.list_recently_removed_symbols(conn, settings.AI_REBALANCE_REMOVED_COOLDOWN_DAYS)

        # Case 1: 점수 0.42 (< 0.45) -> 쿨다운 면제 대상 아님 -> 트리거 발생 안 함 (None)
        await db.set_state(
            conn,
            discovery.STATE_SCORED_CACHE,
            json.dumps({
                "scored_at": time.time(),
                "entries": [
                    {"symbol": "088980", "name": "맥쿼리인프라", "score": 0.42, "angle": "dividend", "angle_label": "배당"}
                ],
            }, ensure_ascii=False),
        )
        detail_low = await scheduler._discovery_trigger_detail(conn)
        assert detail_low is None

        # Case 2: 점수 0.48 (>= 0.45) -> 방안 2 고득점 면제 -> 즉시 발굴 트리거 발동
        await db.set_state(
            conn,
            discovery.STATE_SCORED_CACHE,
            json.dumps({
                "scored_at": time.time(),
                "entries": [
                    {"symbol": "088980", "name": "맥쿼리인프라", "score": 0.48, "angle": "dividend", "angle_label": "배당"}
                ],
            }, ensure_ascii=False),
        )
        detail_high = await scheduler._discovery_trigger_detail(conn)
        assert detail_high is not None
        assert "088980" in detail_high
        assert "맥쿼리인프라" in detail_high

        await conn.close()
    asyncio.run(run())


def test_removed_symbol_high_score_exemption_in_candidate_pool_screening():
    """후보 종목 스크리닝(propose_rebalance)에서도 고득점(>= 0.45) 발굴 종목은 exclude_symbols에서 면제된다."""
    async def run():
        conn = await _conn()

        # 활성 포트폴리오 기본 종목 등록 (포트폴리오 비어있음 방지)
        await db.add_portfolio_symbol(conn, "005930")
        pid = await db.propose_target_weight(conn, "005930", 0.2, "manual")
        await db.decide_target_weight(conn, pid, True)

        await db.add_candidate_symbol(conn, "088980", "맥쿼리인프라", "DYNAMIC")
        await db.add_portfolio_symbol(conn, "088980")
        await db.finish_symbol_removal(conn, "088980")

        # 발굴 캐시에 0.48 점수로 등록
        await db.set_state(
            conn,
            discovery.STATE_SCORED_CACHE,
            json.dumps({
                "scored_at": time.time(),
                "entries": [
                    {"symbol": "088980", "name": "맥쿼리인프라", "score": 0.48, "angle": "dividend", "angle_label": "배당", "universe_tag": "DIVIDEND"}
                ],
            }, ensure_ascii=False),
        )

        mock_client = AsyncMock()
        mock_provider = AsyncMock()
        mock_provider.propose_portfolio_changes.return_value = {
            "weights": {"005930": 0.2, "088980": 0.1},
            "adds": [{"symbol": "088980", "rationale": "우수 고배당 발굴"}],
            "removes": [],
            "rationale": "재편입",
        }

        with patch("core.portfolio_agent.get_llm_provider", return_value=mock_provider), \
             patch("core.discovery.validate_candidate_symbol", new_callable=AsyncMock) as mock_val:
            mock_val.return_value = discovery.ValidationOutcome(True, "OK")
            res = await portfolio_agent.propose_rebalance(
                conn, mock_client, trigger_type="DISCOVERY", trigger_detail="088980",
            )
            assert res["status"] == "PROPOSED"
            # candidate_pool에 088980이 전달되었는지 확인
            called_pool = mock_provider.propose_portfolio_changes.call_args[1]["candidate_pool"]
            assert any(c["symbol"] == "088980" for c in called_pool)

        await conn.close()
    asyncio.run(run())


def test_engine_cleans_up_removed_symbol_when_liquidated():
    """엔진 매매 루프에서 비중 0% & 잔고 0인 종목은 finish_symbol_removal을 통해 깔끔히 정리되어야 한다."""
    from core.engine import TradingEngine
    from core.risk import RiskManager

    async def run():
        conn = await _conn()
        await db.set_state(conn, "domestic_only", "1")

        # 088980 목표비중 0.0 및 portfolio_symbols 등록
        await db.add_portfolio_symbol(conn, "088980")
        pid = await db.propose_target_weight(conn, "088980", 0.0, "llm")
        await db.decide_target_weight(conn, pid, True)

        active = await db.get_active_target_weights(conn)
        assert "088980" in active
        assert active["088980"] == 0.0

        mock_client = AsyncMock()
        engine = TradingEngine()
        engine.conn = conn
        engine.client = mock_client
        engine.risk = RiskManager(conn)

        with patch("core.engine._is_krx_open_now", return_value=True), \
             patch("core.engine._is_market_open", return_value=True), \
             patch("core.kis_domestic.get_balance", new_callable=AsyncMock) as mock_bal, \
             patch("core.reconciliation.reconcile_unresolved_intents", new_callable=AsyncMock):
            mock_bal.return_value = {"holdings": [], "summary": {"nass_amt": 10000000}}
            await engine._run_one_cycle()

        # 사이클 실행 후 잔고가 없으므로 finish_symbol_removal이 호출되어 active_target_weights에서 제거되고
        # portfolio_symbols는 disabled_at이 설정되어 recently_removed에 등재되어야 함
        active_after = await db.get_active_target_weights(conn)
        assert "088980" not in active_after

        recently_removed = await db.list_recently_removed_symbols(conn, 7)
        assert "088980" in recently_removed

        await conn.close()
    asyncio.run(run())
