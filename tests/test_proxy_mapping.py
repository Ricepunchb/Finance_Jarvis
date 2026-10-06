# tests/test_proxy_mapping.py
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

import aiosqlite
import pytest

from core import proxy_mapping, db
from core.config import get_effective_proxy_trading, set_effective_proxy_trading
from core.engine import TradingEngine
from api.main import app


def test_load_proxy_mappings():
    mappings = proxy_mapping.load_proxy_mappings(force_reload=True)
    assert "GOOGL" in mappings
    assert "NVDA" in mappings
    assert "GLD" in mappings
    assert "LLY" in mappings
    assert "VST" in mappings
    assert mappings["GOOGL"]["proxy_symbol"] == "473460"
    assert mappings["GLD"]["proxy_symbol"] == "132030"


def test_get_proxy_etf_case_insensitive():
    info_upper = proxy_mapping.get_proxy_etf("GOOGL")
    info_lower = proxy_mapping.get_proxy_etf("googl")
    assert info_upper is not None
    assert info_lower is not None
    assert info_upper["proxy_symbol"] == "473460"
    assert info_lower["proxy_name"] == "ACE 구글밸류체인액티브"


def test_get_reverse_proxy_targets():
    targets = proxy_mapping.get_reverse_proxy_targets("483320")
    # 483320은 미국 AI 전력인프라 ETF로 VST, CEG, GEV, ETN 등이 매핑됨
    assert "VST" in targets
    assert "CEG" in targets


def test_resolve_order_symbol():
    # 프록시 모드 ON
    sym, proxied, info = proxy_mapping.resolve_order_symbol("GOOGL", proxy_enabled=True)
    assert sym == "473460"
    assert proxied is True
    assert info["proxy_name"] == "ACE 구글밸류체인액티브"

    # 프록시 모드 OFF
    sym_off, proxied_off, info_off = proxy_mapping.resolve_order_symbol("GOOGL", proxy_enabled=False)
    assert sym_off == "GOOGL"
    assert proxied_off is False
    assert info_off is None

    # 매핑 없는 일반 국내종목
    sym_dom, proxied_dom, info_dom = proxy_mapping.resolve_order_symbol("005930", proxy_enabled=True)
    assert sym_dom == "005930"
    assert proxied_dom is False
    assert info_dom is None


def test_get_set_effective_proxy_trading():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(db.SCHEMA)

            # 기본값 확인
            val = await get_effective_proxy_trading(conn)
            assert val is True  # 기본값 True

            # 비활성화
            await set_effective_proxy_trading(conn, False)
            val_off = await get_effective_proxy_trading(conn)
            assert val_off is False

            # 다시 활성화
            await set_effective_proxy_trading(conn, True)
            val_on = await get_effective_proxy_trading(conn)
            assert val_on is True

    asyncio.run(run())


def test_api_proxy_trading_endpoints():
    async def run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # GET mappings
            resp_map = await ac.get("/engine/proxy-mappings")
            assert resp_map.status_code == 200
            mappings = resp_map.json()
            assert "GOOGL" in mappings

            # POST proxy-trading toggle
            resp_post = await ac.post("/engine/proxy-trading", json={"enabled": False})
            assert resp_post.status_code == 200
            assert resp_post.json()["proxy_trading_enabled"] is False

            # GET config 확인
            resp_cfg = await ac.get("/engine/config")
            assert resp_cfg.status_code == 200
            assert resp_cfg.json()["proxy_trading_enabled"] is False

            # 다시 원복
            resp_post2 = await ac.post("/engine/proxy-trading", json={"enabled": True})
            assert resp_post2.status_code == 200
            assert resp_post2.json()["proxy_trading_enabled"] is True

    asyncio.run(run())


def test_engine_proxy_order_routing():
    """_process_overseas_symbol에서 action 발생 시 국내 대체 ETF로 주문이 라우팅되는지 검증"""
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(db.SCHEMA)
            await set_effective_proxy_trading(conn, True)

            engine = TradingEngine()
            engine.conn = conn
            engine.risk = MagicMock()
            engine.risk.is_cooldown_active = AsyncMock(return_value=False)
            engine.risk.is_daily_loss_limit_hit = AsyncMock(return_value=False)

            # KIS 및 외부 호출 모킹
            mock_chart = [{
                "xymd": "20261001", "open": "195", "high": "205", "low": "190", "clos": "200", "tvol": "10000"
            }]
            with patch("core.kis_overseas.get_price", new=AsyncMock(return_value={"last": "200.0"})), \
                 patch("core.kis_overseas.get_daily_chart", new=AsyncMock(return_value=mock_chart)), \
                 patch("core.intraday.get_intraday_signal_overseas", new=AsyncMock(return_value=None)), \
                 patch("core.fundamentals.valuation.get_valuation_signal_overseas", new=AsyncMock(return_value=None)), \
                 patch("core.signal_engine.decide", new=AsyncMock(return_value=MagicMock(side="buy", qty=10, reason="GOOGL 모멘텀 매수", order_type="limit"))), \
                 patch("core.kis_domestic.get_price", new=AsyncMock(return_value={"stck_prpr": "15000"})), \
                 patch("core.kis_domestic.get_buyable_cash", new=AsyncMock(return_value={"max_buy_qty": 100})), \
                 patch("core.kis_domestic.order_cash", new=AsyncMock(return_value={"ODNO": "MOCK_ODNO_123"})) as mock_dom_order:


                await engine._process_overseas_symbol(
                    cycle_id=1,
                    symbol="GOOGL",
                    exchange="NASD",
                    target_weight=0.1,
                    holding={"bass_exrt": 1400.0, "cblc_qty13": 0},
                    total_equity=100_000_000.0,
                    ws_ok=True,
                )

                # 국내 order_cash가 ACE 구글밸류체인(473460)으로 호출되었는지 검증!
                assert mock_dom_order.called
                call_args = mock_dom_order.call_args[0]
                assert call_args[1] == "473460"  # proxy_symbol
                assert call_args[2] == "buy"

    asyncio.run(run())


def test_portfolio_agent_proxy_rebalance_mapping():
    """AI 리밸런싱 제안 시 해외 원주가 국장 대체 ETF로 자동 매핑되는지 검증"""
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await conn.executescript(db.SCHEMA)
            await db._migrate_add_missing_columns(conn)
            await set_effective_proxy_trading(conn, True)



            # 기존 보유 종목 등록 (각각 30% 이하)
            pid1 = await db.propose_target_weight(conn, "005930", 0.25, "user", "초기 비중")
            await db.decide_target_weight(conn, pid1, True)
            pid2 = await db.propose_target_weight(conn, "000660", 0.25, "user", "초기 비중")
            await db.decide_target_weight(conn, pid2, True)

            # LLM이 GOOGL을 신규 편입(adds 10%)으로 제안하도록 모킹 (변화폭 <= 10%)
            mock_llm_result = {
                "adds": [{"symbol": "GOOGL", "name": "Alphabet Inc.", "rationale": "AI 검색 모멘텀 강력"}],
                "removes": [],
                "weights": {"005930": 0.20, "000660": 0.20, "GOOGL": 0.10},
                "rationale": "빅테크 분산 투자 제안",
            }





            from core import portfolio_agent
            with patch("core.portfolio_agent.get_llm_provider") as mock_get_llm, \
                 patch("core.discovery.screen_candidates", new=AsyncMock(return_value=[{
                     "symbol": "GOOGL", "name": "Alphabet Inc.", "proxy_symbol": "473460", "proxy_name": "ACE 구글밸류체인액티브"
                 }])), \
                 patch("core.discovery.validate_candidate_symbol", new=AsyncMock(return_value=MagicMock(ok=True, kis_name="ACE 구글밸류체인액티브"))):
                mock_provider = MagicMock()
                mock_provider.propose_portfolio_changes = AsyncMock(return_value=mock_llm_result)
                mock_get_llm.return_value = mock_provider

                res = await portfolio_agent.propose_rebalance(conn, client=AsyncMock(), trigger_type="MANUAL")
                assert res["status"] == "PROPOSED", f"Rebalance failed with reason: {res.get('reason')}"


                # adds가 473460으로 자동 변환되었는지 검증!
                assert "473460" in res["adds"]
                assert "GOOGL" not in res["adds"]

    asyncio.run(run())

