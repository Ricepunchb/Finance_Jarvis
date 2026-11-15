import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from core import db, discovery, portfolio_agent, signal_engine
from core.ai_rebalance_guard import compute_effective_max_symbols, validate_proposal
from core.config import settings
from core.llm.base import LLMProvider
from core.risk import RiskManager


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(db.SCHEMA)
    await db._migrate_add_missing_columns(conn)
    return conn


def test_compute_effective_max_symbols_scales_with_equity():
    assert compute_effective_max_symbols(20, None, 250_000, 4) == 20
    assert compute_effective_max_symbols(20, 0.0, 250_000, 4) == 20
    # ISA (~66만원) -> 하한 4종목
    assert compute_effective_max_symbols(20, 662_984.0, 250_000, 4) == 4
    # 실전 일반 (~243만원) -> 9종목
    assert compute_effective_max_symbols(20, 2_427_954.0, 250_000, 4) == 9
    # 모의투자 (~1,011만원) -> 기본 상한 20종목 유지
    assert compute_effective_max_symbols(20, 10_111_099.0, 250_000, 4) == 20


def test_validate_proposal_blocks_unaffordable_add_and_unheld_increase():
    active = {"088980": 0.25, "034230": 0.25}
    # 1) 신규 편입 005300(주가 101,200원)을 66만원 계좌에 10%(66,000원)로 편입 시도 -> 반려
    res = validate_proposal(
        {"088980": 0.25, "034230": 0.25, "005300": 0.10},
        active,
        adds=["005300"],
        removes=[],
        max_turnover_pct=0.20,
        max_weight_delta_pct=0.10,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
        total_equity=660_000.0,
        symbol_prices={"088980": 9_820.0, "034230": 10_280.0, "005300": 101_200.0},
        held_quantities={"088980": 4.0, "034230": 9.0, "005300": 0.0},
    )
    assert not res.ok
    assert "1주 매수 불가" in res.reason

    # 2) 저가 종목 411060(주가 23,345원)을 10%(66,000원)로 편입 -> 통과
    res_ok = validate_proposal(
        {"088980": 0.25, "034230": 0.25, "411060": 0.10},
        active,
        adds=["411060"],
        removes=[],
        max_turnover_pct=0.20,
        max_weight_delta_pct=0.10,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
        total_equity=660_000.0,
        symbol_prices={"088980": 9_820.0, "034230": 10_280.0, "411060": 23_345.0},
        held_quantities={"088980": 4.0, "034230": 9.0, "411060": 0.0},
    )
    assert res_ok.ok

    # 3) 기존 미보유 고가주(028050, 68,900원)의 비중을 5% -> 8%(52,800원)로 올렸지만 여전히 1주 미만 -> 반려
    res_inc = validate_proposal(
        {"088980": 0.25, "034230": 0.25, "028050": 0.08},
        {"088980": 0.25, "034230": 0.25, "028050": 0.05},
        adds=[],
        removes=[],
        max_turnover_pct=0.20,
        max_weight_delta_pct=0.10,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
        total_equity=660_000.0,
        symbol_prices={"088980": 9_820.0, "034230": 10_280.0, "028050": 68_900.0},
        held_quantities={"088980": 4.0, "034230": 9.0, "028050": 0.0},
    )
    assert not res_inc.ok
    assert "1주 매수 불가" in res_inc.reason


def test_validate_proposal_gradual_shrink_when_already_oversized():
    # 현재 6종목 보유 중이나 자금 상한은 4종목인 경우
    active = {f"S{i}": 0.10 for i in range(6)}

    # 1) 신규 편입 시도 -> 차단
    res_add = validate_proposal(
        {**active, "NEW1": 0.10},
        active,
        adds=["NEW1"],
        removes=[],
        max_turnover_pct=0.20,
        max_weight_delta_pct=0.10,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
    )
    assert not res_add.ok
    assert "신규 편입 불가" in res_add.reason

    # 2) 아무것도 줄이지 않고 6종목 유지 -> 차단
    res_keep = validate_proposal(
        dict(active),
        active,
        adds=[],
        removes=[],
        max_turnover_pct=0.20,
        max_weight_delta_pct=0.10,
        max_symbols_added=0,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
    )
    assert not res_keep.ok
    assert "줄여야 함" in res_keep.reason

    # 3) 2종목 제외(6 -> 4) -> 통과 (데드락 없음)
    shrunk = {f"S{i}": 0.10 for i in range(4)}
    shrunk["S4"] = 0.0
    shrunk["S5"] = 0.0
    res_shrink = validate_proposal(
        shrunk,
        active,
        adds=[],
        removes=["S4", "S5"],
        max_turnover_pct=0.20,
        max_weight_delta_pct=0.10,
        max_symbols_added=0,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
    )
    assert res_shrink.ok


def test_screen_candidates_filters_unaffordable_prices(monkeypatch):
    async def run():
        conn = await _conn()
        monkeypatch.setattr(settings, "DOMESTIC_ONLY", True)
        await db.add_candidate_symbol(conn, "088980", "맥쿼리인프라", "VALUE")
        await db.add_candidate_symbol(conn, "005300", "롯데칠성", "VALUE")
        await db.add_candidate_symbol(conn, "068270", "셀트리온", "MOMENTUM")
        await db.set_state(
            conn,
            discovery.STATE_SCORED_CACHE,
            json.dumps({
                "scored_at": time.time(),
                "entries": [
                    {"symbol": "088980", "name": "맥쿼리인프라", "universe_tag": "VALUE", "score": 0.70, "angle_label": "저평가", "last_close": 9_820.0, "raw_components": {"tech": 0.7, "value": 0.7, "early": 0.5, "broker": 0.5}},
                    {"symbol": "005300", "name": "롯데칠성", "universe_tag": "VALUE", "score": 0.80, "angle_label": "저평가", "last_close": 101_200.0, "raw_components": {"tech": 0.8, "value": 0.8, "early": 0.5, "broker": 0.5}},
                    {"symbol": "068270", "name": "셀트리온", "universe_tag": "MOMENTUM", "score": 0.85, "angle_label": "모멘텀", "last_close": 213_500.0, "raw_components": {"tech": 0.8, "value": 0.5, "early": 0.8, "broker": 0.5}},
                ],
            }),
        )
        chosen = await discovery.screen_candidates(
            conn, client=AsyncMock(), exclude_symbols=[], max_affordable_price=66_000.0
        )
        syms = {c["symbol"] for c in chosen}
        assert "088980" in syms
        assert "005300" not in syms
        assert "068270" not in syms
        await conn.close()

    asyncio.run(run())


class _FakeLLM(LLMProvider):
    def __init__(self, reply):
        self.reply = reply
        self.last_kwargs = {}

    async def analyze_news(self, symbol, title, summary):
        return {}

    async def propose_weights(self, symbols):
        return {}

    async def propose_portfolio_changes(self, **kwargs):
        self.last_kwargs = kwargs
        return self.reply

    async def analyze_research_reports(self, reports):
        return {}

    async def summarize_insight_batch(self, context):
        return {}


def test_propose_rebalance_enforces_capital_cap_and_add_affordability(monkeypatch):
    async def run():
        conn = await _conn()
        monkeypatch.setattr(settings, "BROKER", "kis")
        monkeypatch.setattr(settings, "AI_REBALANCE_AUTO_APPLY", False)
        monkeypatch.setattr(settings, "AI_REBALANCE_MIN_POSITION_VALUE_KRW", 250_000)
        monkeypatch.setattr(settings, "AI_REBALANCE_MIN_PORTFOLIO_SYMBOLS_FLOOR", 4)

        await db.add_portfolio_symbol(conn, "088980", market="domestic")
        pid = await db.propose_target_weight(conn, "088980", 0.25, "manual")
        await db.decide_target_weight(conn, pid, True)
        await db.upsert_position(conn, "088980", qty=10, avg_price=9500)

        fake_client = AsyncMock()
        # 1) 초고가주(068270, 213,500원)는 최대 초기비중(25%=165,000원)으로도 매수 불가 -> _validate_adds에서 거부됨
        fake_llm = _FakeLLM({
            "adds": [{"symbol": "068270", "name": "셀트리온", "rationale": "테스트"}],
            "removes": [],
            "weights": {"088980": 0.25, "068270": 0.25},
            "rationale": "편입 시도",
            "degraded": False,
        })
        candidate_pool = [
            {"symbol": "068270", "name": "셀트리온", "last_close": 213_500.0},
        ]

        with patch("core.kis_domestic.get_balance", new=AsyncMock(return_value={"summary": {"nass_amt": "660000"}, "holdings": []})), \
             patch("core.quant_metrics.fetch_benchmark_return_series", new=AsyncMock(return_value=None)), \
             patch("core.kis_domestic.get_daily_chart", new=AsyncMock(return_value=[])), \
             patch("core.discovery.screen_candidates", new=AsyncMock(return_value=candidate_pool)), \
             patch("core.discovery.validate_candidate_symbol", new=AsyncMock(return_value=discovery.ValidationOutcome(True, kis_name="셀트리온", price=213_500.0))), \
             patch("core.macro.get_fear_greed_context", new=AsyncMock(return_value=None)), \
             patch("core.portfolio_agent.get_llm_provider", return_value=fake_llm):
            res = await portfolio_agent.propose_rebalance(conn, client=fake_client, trigger_type="MANUAL")

        # 66만원 계좌이므로 max_symbols가 4로 축소되어 LLM에 전달됨
        assert fake_llm.last_kwargs["max_symbols"] == 4
        assert fake_llm.last_kwargs["total_equity"] == 660_000.0
        # 고가주 편입은 거부되어 adds가 빈 리스트가 되고, 088980만 남아서 PROPOSED됨
        assert res["adds"] == []
        ev = await db.get_rebalance_event(conn, res["event_id"])
        assert ev is not None
        assert "1주 매수 불가" in ev["context_json"]

        # 2) 중가주(005300, 101,200원)를 10%(66,000원) 부족 비중으로 편입 시도 -> validate_proposal에서 BLOCKED
        fake_llm_under = _FakeLLM({
            "adds": [{"symbol": "005300", "name": "롯데칠성", "rationale": "테스트"}],
            "removes": [],
            "weights": {"088980": 0.25, "005300": 0.10},
            "rationale": "소액 비중 편입 시도",
            "degraded": False,
        })
        with patch("core.kis_domestic.get_balance", new=AsyncMock(return_value={"summary": {"nass_amt": "660000"}, "holdings": []})), \
             patch("core.quant_metrics.fetch_benchmark_return_series", new=AsyncMock(return_value=None)), \
             patch("core.kis_domestic.get_daily_chart", new=AsyncMock(return_value=[])), \
             patch("core.discovery.screen_candidates", new=AsyncMock(return_value=[{"symbol": "005300", "name": "롯데칠성", "last_close": 101_200.0}])), \
             patch("core.discovery.validate_candidate_symbol", new=AsyncMock(return_value=discovery.ValidationOutcome(True, kis_name="롯데칠성", price=101_200.0))), \
             patch("core.macro.get_fear_greed_context", new=AsyncMock(return_value=None)), \
             patch("core.portfolio_agent.get_llm_provider", return_value=fake_llm_under):
            res_blocked = await portfolio_agent.propose_rebalance(conn, client=fake_client, trigger_type="MANUAL")
        assert res_blocked["status"] == "BLOCKED"
        assert "1주 매수 불가" in res_blocked["reason"]
        await conn.close()

    asyncio.run(run())


def test_onboarding_allows_small_account_single_share_and_5pct_target(monkeypatch):
    async def run():
        conn = await _conn()
        risk = RiskManager(conn)
        monkeypatch.setattr(settings, "ONBOARDING_ENABLED", True)
        monkeypatch.setattr(settings, "ONBOARDING_DAYS", 5)
        monkeypatch.setattr(settings, "REQUIRE_UPTREND_FOR_ONBOARDING", False)
        monkeypatch.setattr(settings, "REQUIRE_SMA20_FOR_BUY", False)

        # 1) target_weight=5%, band=5%, 보유=0주, total_equity=2,000,000원(5%=100,000원 >= 45,000원) -> 온보딩 시작됨
        act1 = await signal_engine.decide(
            symbol="035900",
            current_weight=0.0,
            target_weight=0.05,
            band=0.05,
            tech_signal={"direction": "HOLD", "strength": 0.1},
            intraday_signal=None,
            sentiment_signal=None,
            price=45_000.0,
            current_position_qty=0.0,
            current_position_value=0.0,
            total_equity=2_000_000.0,
            risk=risk,
        )
        assert act1 is not None
        assert act1.side == "buy"
        assert act1.qty == 1.0
        assert act1.reason == "onboarding_tranche"

        # 같은 날 이미 온보딩 매수 1건이 있으면 당일 추가 매수는 하지 않음
        await db.create_order_intent(conn, 1, "035900", "buy", 1.0, "limit", 45_000.0, "onboarding_tranche")
        act_same_day = await signal_engine.decide(
            symbol="035900",
            current_weight=0.0225,
            target_weight=0.05,
            band=0.05,
            tech_signal={"direction": "HOLD", "strength": 0.1},
            intraday_signal=None,
            sentiment_signal=None,
            price=45_000.0,
            current_position_qty=1.0,
            current_position_value=45_000.0,
            total_equity=2_000_000.0,
            risk=risk,
        )
        assert act_same_day is None

        # 2) 목표 금액(33,000원) < 1주 가격(68,900원)이면 여전히 1주도 매수하지 않음
        act_unaffordable = await signal_engine.decide(
            symbol="028050",
            current_weight=0.0,
            target_weight=0.05,
            band=0.05,
            tech_signal={"direction": "HOLD", "strength": 0.1},
            intraday_signal=None,
            sentiment_signal=None,
            price=68_900.0,
            current_position_qty=0.0,
            current_position_value=0.0,
            total_equity=660_000.0,
            risk=risk,
        )
        assert act_unaffordable is None
        await conn.close()

    asyncio.run(run())


def test_scenario_full_100pct_small_portfolio_can_swap_promising_mid_price_stock(monkeypatch):
    """시나리오 1: 소액 계좌(66만원, 4종목×25%=100% 꽉 참)에서 기존 1종목을 빼고 10만원대 유망 종목으로 1:1 교체 가능해야 함."""
    async def run():
        conn = await _conn()
        monkeypatch.setattr(settings, "BROKER", "kis")
        monkeypatch.setattr(settings, "AI_REBALANCE_AUTO_APPLY", False)
        monkeypatch.setattr(settings, "AI_REBALANCE_MIN_POSITION_VALUE_KRW", 250_000)
        monkeypatch.setattr(settings, "AI_REBALANCE_MIN_PORTFOLIO_SYMBOLS_FLOOR", 4)

        for sym in ["088980", "034230", "035900", "411060"]:
            await db.add_portfolio_symbol(conn, sym, market="domestic")
            pid = await db.propose_target_weight(conn, sym, 0.25, "manual")
            await db.decide_target_weight(conn, pid, True)

        fake_client = AsyncMock()
        # 102,415원짜리 채권 ETF(454780)를 25%(165,000원)로 교체 편입 (035900 제외)
        fake_llm = _FakeLLM({
            "adds": [{"symbol": "454780", "name": "KIWOOM 종합채권", "rationale": "시황 방어 교체"}],
            "removes": [{"symbol": "035900", "rationale": "모멘텀 둔화 교체"}],
            "weights": {"088980": 0.25, "034230": 0.25, "411060": 0.25, "035900": 0.0, "454780": 0.25},
            "rationale": "100% 꽉 찬 4종목 포트폴리오에서 1:1 교체",
            "degraded": False,
        })
        candidate_pool = [{"symbol": "454780", "name": "KIWOOM 종합채권", "last_close": 102_415.0}]

        with patch("core.kis_domestic.get_balance", new=AsyncMock(return_value={"summary": {"nass_amt": "660000"}, "holdings": []})), \
             patch("core.quant_metrics.fetch_benchmark_return_series", new=AsyncMock(return_value=None)), \
             patch("core.kis_domestic.get_daily_chart", new=AsyncMock(return_value=[])), \
             patch("core.discovery.screen_candidates", new=AsyncMock(return_value=candidate_pool)) as mock_screen, \
             patch("core.discovery.validate_candidate_symbol", new=AsyncMock(return_value=discovery.ValidationOutcome(True, kis_name="KIWOOM 종합채권", price=102_415.0))), \
             patch("core.macro.get_fear_greed_context", new=AsyncMock(return_value=None)), \
             patch("core.portfolio_agent.get_llm_provider", return_value=fake_llm):
            res = await portfolio_agent.propose_rebalance(conn, client=fake_client, trigger_type="MANUAL")

        # 4종목 계좌이므로 screen_candidates의 max_affordable_price도 25%(165,000원)로 열려야 함
        assert mock_screen.call_args.kwargs["max_affordable_price"] == pytest.approx(165_000.0)
        assert res["status"] == "PROPOSED"
        assert res["adds"] == ["454780"]
        assert res["removes"] == ["035900"]
        await conn.close()

    asyncio.run(run())


def test_scenario_full_100pct_large_portfolio_trims_existing_weights_to_add_new():
    """시나리오 2: 종목 수 여유는 있지만 비중 합이 100%로 꽉 찬 경우 기존 종목 비중을 낮춰 신규 편입 가능."""
    active = {f"S{i}": 0.10 for i in range(10)}  # 10종목 × 10% = 100%
    proposed = {**active, "S0": 0.05, "S1": 0.05, "NEW1": 0.10}
    res = validate_proposal(
        proposed,
        active,
        adds=["NEW1"],
        removes=[],
        max_turnover_pct=0.20,
        max_weight_delta_pct=0.10,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=20,
        total_equity=10_000_000.0,
        symbol_prices={**{f"S{i}": 50_000.0 for i in range(10)}, "NEW1": 200_000.0},
    )
    assert res.ok


def test_scenario_oversized_portfolio_allows_swap_only_when_shrinking_into_cap():
    """시나리오 3: 상한(4종목)을 1개 초과(5종목) 중일 때, 2개 빼고 1개 넣어 4종목으로 맞추는 교체는 허용되고 5종목 유지는 차단됨."""
    active = {f"S{i}": 0.15 for i in range(5)}

    # 2개 제외 + 1개 편입 -> 최종 4종목(<=4), turnover=(0.30+0.20)/2=0.25 -> 허용
    res_ok = validate_proposal(
        {"S0": 0.15, "S1": 0.15, "S2": 0.15, "S3": 0.0, "S4": 0.0, "NEW1": 0.20},
        active,
        adds=["NEW1"],
        removes=["S3", "S4"],
        max_turnover_pct=0.25,
        max_weight_delta_pct=0.10,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
        max_add_weight_pct=0.25,
    )
    assert res_ok.ok

    # 1개 제외 + 1개 편입 -> 최종 5종목(>4) -> 차단
    res_blocked = validate_proposal(
        {"S0": 0.15, "S1": 0.15, "S2": 0.15, "S3": 0.15, "S4": 0.0, "NEW1": 0.20},
        active,
        adds=["NEW1"],
        removes=["S4"],
        max_turnover_pct=0.25,
        max_weight_delta_pct=0.10,
        max_symbols_added=2,
        max_symbols_removed=2,
        min_symbol_weight_pct=0.03,
        max_symbol_weight_pct=0.30,
        max_portfolio_symbols=4,
        max_add_weight_pct=0.25,
    )
    assert not res_blocked.ok
    assert "신규 편입 불가" in res_blocked.reason

