# api/main.py
"""자동매매 엔진 제어 플레인. 엔진은 이 프로세스 안에서 /engine/start 호출 시에만
백그라운드 asyncio 태스크로 기동한다 (프로세스 부팅 시 자동시작 안 함).

실행: uvicorn api.main:app --port 8800
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from core import (
    analytics, db, discovery, discovery_signals, discovery_sources, kis_domestic, portfolio_agent, signal_engine,
)
from core.config import settings
from core.engine import CYCLE_INTERVAL_SEC, TradingEngine
from core.lock import EngineAlreadyRunningError
from core.portfolio_scheduler import PortfolioScheduler
from core.risk import RiskManager

engine = TradingEngine()
# TradingEngine과 별개의 라이프사이클로 도는 AI 포트폴리오 에이전트 트리거 루프.
# engine.client를 그대로 공유한다 (KIS 토큰 재발급마다 카카오톡 알림이 가므로, 별도
# 클라이언트를 새로 만들어 불필요한 토큰을 추가 발급받지 않기 위함).
portfolio_scheduler = PortfolioScheduler(engine.client)

# 국내 종목 한글명 캐시. 이름은 사실상 불변이므로 프로세스 수명 동안 재조회하지 않는다
# (엔진의 KIS 클라이언트를 그대로 재사용 — 대시보드 표시용으로 별도 토큰을 새로 발급하지 않기 위함).
_symbol_name_cache: dict[str, str] = {}

logger = logging.getLogger(__name__)
# create_task 결과를 참조로 붙잡아 두지 않으면 실행 도중 GC될 수 있다
_discovery_refresh_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    portfolio_scheduler.start()
    yield
    await portfolio_scheduler.stop()


app = FastAPI(title="Finance Jarvis - KIS 자동매매 제어", lifespan=lifespan)


class AddSymbolRequest(BaseModel):
    symbol: str
    market: str = "domestic"
    exchange: Optional[str] = None  # market="overseas"일 때만: NASD/NYSE/AMEX 등


class SetWeightRequest(BaseModel):
    symbol: str
    weight: float


class DecideProposalRequest(BaseModel):
    approve: bool


class AddCandidateRequest(BaseModel):
    symbol: str
    name: Optional[str] = None  # 대조용 - 생략 시 KIS 조회 결과를 그대로 신뢰


class SchedulerToggleRequest(BaseModel):
    enabled: Optional[bool] = None
    auto_apply: Optional[bool] = None


@app.post("/engine/start")
async def start_engine():
    try:
        await engine.start()
    except EngineAlreadyRunningError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:  # 실전투자 이중가드 미충족 등
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "started"}


@app.post("/engine/stop")
async def stop_engine():
    await engine.stop()
    return {"status": "stopped"}


@app.post("/engine/kill")
async def kill_engine():
    """비상정지: 신규주문 차단 + 미체결 주문 전량취소 + 정지."""
    await engine.kill()
    return {"status": "killed"}


@app.post("/engine/clear-kill-switch")
async def clear_kill_switch():
    """자동 복구는 없다 — 사람이 명시적으로 호출해야만 kill switch가 해제된다."""
    conn = await db.get_connection()
    try:
        await RiskManager(conn).clear_kill_switch()
    finally:
        await conn.close()
    return {"status": "kill_switch_cleared"}


@app.get("/engine/status")
async def engine_status():
    conn = await db.get_connection()
    try:
        heartbeat_at = await db.get_state(conn, "heartbeat_at")
        last_ws = await db.get_state(conn, "last_ws_message_at")
        return {
            "is_mock": settings.IS_MOCK,
            "engine_running": (await db.get_state(conn, "engine_running")) == "1",
            "pid": await db.get_state(conn, "pid"),
            "heartbeat_age_sec": (time.time() - float(heartbeat_at)) if heartbeat_at else None,
            "ws_last_message_age_sec": (time.time() - float(last_ws)) if last_ws else None,
            "kill_switch_active": (await db.get_state(conn, "kill_switch_active")) == "1",
            "kill_switch_reason": await db.get_state(conn, "kill_switch_reason"),
        }
    finally:
        await conn.close()


@app.get("/engine/config")
async def get_config():
    """UI가 리스크/LLM 설정값을 보여주기 위한 읽기전용 엔드포인트. 비밀값(API 키 원문)은 노출하지 않는다."""
    return {
        "is_mock": settings.IS_MOCK,
        "domestic_only": settings.DOMESTIC_ONLY,
        "cycle_interval_sec": CYCLE_INTERVAL_SEC,
        "rebalance_band_pct": settings.REBALANCE_BAND_PCT,
        "max_position_pct": settings.MAX_POSITION_PCT,
        "max_order_notional_krw": settings.MAX_ORDER_NOTIONAL_KRW,
        "max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT,
        "order_cooldown_sec": settings.ORDER_COOLDOWN_SEC,
        "ws_staleness_threshold_sec": settings.WS_STALENESS_THRESHOLD_SEC,
        "llm_provider": settings.LLM_PROVIDER,
        "gemini_model": settings.GEMINI_MODEL,
        "gemini_fallback_model": settings.GEMINI_FALLBACK_MODEL,
        "gemini_configured": bool(settings.GEMINI_API_KEY),
        "news_lookback_hours": settings.NEWS_LOOKBACK_HOURS,
        "news_max_articles_per_symbol": settings.NEWS_MAX_ARTICLES_PER_SYMBOL,
        "stop_loss_pct": settings.STOP_LOSS_PCT,
        "trailing_take_profit_pct": settings.TRAILING_TAKE_PROFIT_PCT,
        "swing_signal_threshold": signal_engine.SWING_SIGNAL_THRESHOLD,
        "swing_trade_max_equity_fraction": signal_engine.SWING_TRADE_MAX_EQUITY_FRACTION,
        "band_ceiling_buffer_pct": signal_engine.BAND_CEILING_BUFFER_PCT,
        "intraday_bar_minutes": settings.INTRADAY_BAR_MINUTES,
        "intraday_lookback_calendar_days": settings.INTRADAY_LOOKBACK_CALENDAR_DAYS,
        "enable_fundamental_valuation": settings.ENABLE_FUNDAMENTAL_VALUATION,
        "ai_rebalance_max_turnover_pct": settings.AI_REBALANCE_MAX_TURNOVER_PCT,
        "ai_rebalance_max_weight_delta_pct": settings.AI_REBALANCE_MAX_WEIGHT_DELTA_PCT,
        "ai_rebalance_max_symbols_added": settings.AI_REBALANCE_MAX_SYMBOLS_ADDED,
        "ai_rebalance_max_symbols_removed": settings.AI_REBALANCE_MAX_SYMBOLS_REMOVED,
        "ai_rebalance_min_symbol_weight_pct": settings.AI_REBALANCE_MIN_SYMBOL_WEIGHT_PCT,
        "ai_rebalance_max_portfolio_symbols": settings.AI_REBALANCE_MAX_PORTFOLIO_SYMBOLS,
        "discovery_top_n": settings.DISCOVERY_TOP_N,
        "discovery_min_score": settings.DISCOVERY_MIN_SCORE,
        "ai_rebalance_min_interval_sec": settings.AI_REBALANCE_MIN_INTERVAL_SEC,
        "ai_rebalance_periodic_interval_days": settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS,
        "ai_rebalance_drift_trigger_buffer_pct": settings.AI_REBALANCE_DRIFT_TRIGGER_BUFFER_PCT,
        "ai_rebalance_news_trigger_strength": settings.AI_REBALANCE_NEWS_TRIGGER_STRENGTH,
        "risk_free_rate_annual": settings.RISK_FREE_RATE_ANNUAL,
        "winddown_max_days": settings.WINDDOWN_MAX_DAYS,
    }


@app.post("/portfolio/symbols")
async def add_symbol(req: AddSymbolRequest):
    if settings.DOMESTIC_ONLY and req.market != "domestic":
        raise HTTPException(status_code=400, detail="국내 전용 모드(DOMESTIC_ONLY)라 해외 종목은 등록할 수 없습니다")
    conn = await db.get_connection()
    try:
        await db.add_portfolio_symbol(conn, req.symbol, req.market, req.exchange)
    finally:
        await conn.close()
    return {"status": "added", "symbol": req.symbol}


@app.get("/portfolio/symbols")
async def list_symbols():
    conn = await db.get_connection()
    try:
        return await db.list_portfolio_symbols(conn)
    finally:
        await conn.close()


@app.delete("/portfolio/symbols/{symbol}")
async def remove_symbol(symbol: str):
    """포트폴리오에서 종목을 삭제한다. 즉시 매도하지 않는다.

    보유분이 없으면 바로 제거한다. 보유분이 있으면 목표비중을 0으로 내려 신규매수를 막고
    "정리 대기"로 표시한다 — 엔진이 매도 신호가 오거나 WINDDOWN_MAX_DAYS가 지나면 전량 매도하고,
    잔고에서 사라진 것을 확인한 뒤 포트폴리오에서 완전히 제거한다."""
    conn = await db.get_connection()
    try:
        rows = {r["symbol"]: r for r in await db.list_portfolio_symbols(conn)}
        if symbol not in rows:
            raise HTTPException(status_code=404, detail=f"등록된 종목이 아닙니다: {symbol}")
        position = await db.get_position(conn, symbol)
        # 해외 종목은 관리 중일 때만 잔고가 동기화되어 positions가 낡았을 수 있으므로 0이어도 믿지 않는다 —
        # 정리 대기로 넘기고, 엔진이 실제 잔고를 보고 보유가 없으면 제거한다.
        if rows[symbol]["market"] != "overseas" and (not position or float(position["qty"] or 0) <= 0):
            await db.finish_symbol_removal(conn, symbol)
            return {"status": "removed", "symbol": symbol}

        proposal_id = await db.propose_target_weight(
            conn, symbol, 0.0, proposed_by="manual", rationale="사용자 삭제 - 정리 대기(매도 신호 시 전량 매도)",
        )
        await db.decide_target_weight(conn, proposal_id, approve=True)
        await db.start_symbol_winddown(conn, symbol)
    finally:
        await conn.close()
    return {"status": "winding_down", "symbol": symbol, "qty": position["qty"] if position else None}


@app.post("/portfolio/weights")
async def set_weight(req: SetWeightRequest):
    """Phase 1: 목표비중을 수동으로 입력 -> 즉시 승인 (LLM 제안/승인 워크플로는 Phase 2)."""
    conn = await db.get_connection()
    try:
        proposal_id = await db.propose_target_weight(conn, req.symbol, req.weight, proposed_by="manual")
        await db.decide_target_weight(conn, proposal_id, approve=True)
    finally:
        await conn.close()
    return {"status": "approved", "symbol": req.symbol, "weight": req.weight}


@app.get("/portfolio/weights")
async def get_weights():
    conn = await db.get_connection()
    try:
        return await db.get_active_target_weights(conn)
    finally:
        await conn.close()


@app.post("/portfolio/weights/propose")
async def propose_weights_via_llm():
    """AI 포트폴리오 에이전트에게 현재 활성 포트폴리오의 재비중 초안을 요청한다
    (core/portfolio_agent.py — 비중/평단 등 실제 맥락을 넘겨 propose_portfolio_changes를 호출).

    응답은 rebalance_events에 감사로그로 남고, 검증을 통과한 경우에만 PROPOSED 상태의
    target_weights 행이 생성된다. 사람이 /portfolio/weights/proposals/{id}/decide로
    승인해야만 active_target_weights에 반영되어 실제 매매에 쓰인다 (approval_gated 고정).
    """
    if not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY가 설정되지 않았습니다.")

    conn = await db.get_connection()
    try:
        result = await portfolio_agent.propose_rebalance(conn, client=engine.client, trigger_type="MANUAL")
        if result["status"] != "PROPOSED":
            raise HTTPException(status_code=422, detail=result.get("reason", result["status"]))
        return {
            "status": "proposed",
            "event_id": result["event_id"],
            "proposal_ids": result["proposal_ids"],
            "adds": result.get("adds", []),
            "removes": result.get("removes", []),
            "rationale": result.get("rationale", ""),
        }
    finally:
        await conn.close()


@app.get("/portfolio/rebalance-events")
async def list_rebalance_events(limit: int = 50):
    """AI 포트폴리오 에이전트의 리밸런싱 제안 이력 (감사로그 — 승인/거부와 무관하게 전부 기록됨)."""
    conn = await db.get_connection()
    try:
        return await db.list_rebalance_events(conn, limit)
    finally:
        await conn.close()


@app.post("/portfolio/rebalance-events/{event_id}/decide")
async def decide_rebalance_event(event_id: int, req: DecideProposalRequest):
    """이벤트에 묶인 비중변경 + 종목추가/제외를 한 번에 승인/거부한다 (개별 종목 단위인
    /portfolio/weights/proposals/{id}/decide와 달리, 종목 추가가 걸린 제안은 반드시 이
    엔드포인트로 승인해야 portfolio_symbols에도 함께 반영된다)."""
    conn = await db.get_connection()
    try:
        try:
            await db.apply_rebalance_event(conn, event_id, req.approve, decided_by="human")
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    finally:
        await conn.close()
    return {"status": "approved" if req.approve else "rejected", "event_id": event_id}


@app.get("/portfolio/weights/proposals")
async def list_weight_proposals():
    conn = await db.get_connection()
    try:
        return await db.get_pending_weight_proposals(conn)
    finally:
        await conn.close()


@app.post("/portfolio/weights/proposals/{proposal_id}/decide")
async def decide_weight_proposal(proposal_id: int, req: DecideProposalRequest):
    conn = await db.get_connection()
    try:
        await db.decide_target_weight(conn, proposal_id, req.approve)
    finally:
        await conn.close()
    return {"status": "approved" if req.approve else "rejected", "proposal_id": proposal_id}


@app.get("/portfolio/symbol-names")
async def get_symbol_names():
    """국내 종목의 코드->한글종목명 매핑 (대시보드 표시용, 매매 로직과 무관).

    이름은 DB(종목 마스터 -> 후보 풀)에서 먼저 찾고, 거기에 없는 종목만 KIS 시세 조회로 보충한다.
    예전엔 KIS 호출에만 의존해서, 호출이 실패하면(장외/레이트리밋/토큰) 이름이 통째로 빈칸이 됐다.
    비활성 종목과 보유(positions) 종목도 포함한다.
    """
    conn = await db.get_connection()
    try:
        portfolio_rows = await db.list_all_portfolio_symbols(conn)
        positions = await db.get_positions(conn)
        master = await db.get_stock_master(conn)
        cur = await conn.execute("SELECT symbol, name FROM candidate_universe WHERE name IS NOT NULL AND name != ''")
        candidate_names = {r["symbol"]: r["name"] for r in await cur.fetchall()}
    finally:
        await conn.close()

    wanted = {r["symbol"] for r in portfolio_rows if r["market"] == "domestic"}
    wanted |= {sym for sym, p in positions.items() if p.get("currency") == "KRW"}
    names: dict[str, str] = {}
    for symbol in wanted:
        name = (master.get(symbol) or {}).get("name") or candidate_names.get(symbol) or _symbol_name_cache.get(symbol)
        if name:
            names[symbol] = name
    for symbol in wanted - names.keys():  # DB에 없는 종목(드묾)만 KIS로 보충
        try:
            price_info = await kis_domestic.get_price(engine.client, symbol)
            name = price_info.get("hts_kor_isnm")
            if name:
                _symbol_name_cache[symbol] = names[symbol] = name
        except Exception:
            pass
    return names


@app.get("/portfolio/positions")
async def get_positions():
    conn = await db.get_connection()
    try:
        return await db.get_positions(conn)
    finally:
        await conn.close()


@app.get("/portfolio/performance")
async def get_portfolio_performance():
    """보유종목의 성과/리스크/기술 지표(ROI/CAGR/MDD/변동성/샤프/소티노/베타 + RSI/MACD/CCI/BB%B).
    AI 리밸런싱 제안이 LLM에 넘기는 것과 같은 지표를 대시보드 표시용으로 재사용한다
    (매매 판단(signal_engine.decide)에는 전혀 관여하지 않음). 종목마다 일봉 조회가 있어
    보유종목이 많으면 시간이 걸릴 수 있다."""
    conn = await db.get_connection()
    try:
        return await portfolio_agent.get_portfolio_performance(conn, client=engine.client)
    finally:
        await conn.close()


@app.get("/decisions/recent")
async def recent_decisions(limit: int = 50):
    conn = await db.get_connection()
    try:
        return await db.get_recent_decisions(conn, limit)
    finally:
        await conn.close()


# --- 분석 대시보드(app.py)용 읽기 전용 조회. 계산은 core/analytics.py, 여기서는 얇게 감싼다. ---

async def _with_conn(fn, *args, **kwargs):
    conn = await db.get_connection()
    try:
        return await fn(conn, *args, **kwargs)
    finally:
        await conn.close()


@app.get("/analytics/overview")
async def analytics_overview(days: int = 30, refresh: bool = False, fetch_prices: bool = True):
    """성과 오버뷰: KPI + 종목별 요약 + 날짜별 포트폴리오 + 종목x날짜 행렬.
    refresh=true면 일봉 캐시를 무시하고 KIS에서 다시 받는다 (모의계좌는 1 req/s라 오래 걸릴 수 있음)."""
    return await _with_conn(analytics.build_overview, engine.client, days=days, refresh=refresh, fetch_prices=fetch_prices)


@app.get("/analytics/realized")
async def analytics_realized(start: str, end: str, symbol: Optional[str] = None):
    return await _with_conn(analytics.build_realized, start, end, symbol)


@app.get("/analytics/prices")
async def analytics_prices(symbol: str, interval: str = "1d", days: int = 60):
    if interval not in ("1d", "30m"):
        raise HTTPException(status_code=422, detail="interval은 1d 또는 30m 입니다")
    return await _with_conn(analytics.get_prices, engine.client, symbol, interval, days)


@app.get("/analytics/decisions")
async def analytics_decisions(start: str, end: str, symbol: Optional[str] = None, include_noop: bool = False):
    return await _with_conn(analytics.list_decisions, symbol, start, end, include_noop)


@app.get("/analytics/decisions/{decision_id}")
async def analytics_decision_detail(decision_id: int):
    row = await _with_conn(analytics.get_decision_detail, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail="해당 결정 기록이 없습니다")
    return row


@app.get("/analytics/rebalance-history")
async def analytics_rebalance_history(limit: int = 50, with_returns: bool = False):
    return await _with_conn(analytics.build_rebalance_history, engine.client, limit=limit, with_returns=with_returns)


@app.post("/discovery/seed")
async def seed_discovery_universe():
    """data/candidate_universe_seed.json을 KIS로 검증하며 candidate_universe에 적재한다.
    반려된 항목은 조용히 버려지지 않고 결과에 사유와 함께 반환된다."""
    conn = await db.get_connection()
    try:
        return await discovery.seed_candidate_universe(conn, engine.client)
    finally:
        await conn.close()


@app.get("/discovery/candidates")
async def list_discovery_candidates():
    conn = await db.get_connection()
    try:
        return await db.list_candidate_universe(conn)
    finally:
        await conn.close()


@app.post("/discovery/candidates")
async def add_discovery_candidate(req: AddCandidateRequest):
    """사용자가 후보 하나를 수동으로 추가한다 - 시드와 동일하게 KIS 실재성 검증을 거친다."""
    outcome = await discovery.validate_candidate_symbol(engine.client, req.symbol, claimed_name=req.name)
    if not outcome.ok:
        raise HTTPException(status_code=400, detail=outcome.reason)
    resolved_name = outcome.kis_name or req.name or req.symbol
    conn = await db.get_connection()
    try:
        await db.add_candidate_symbol(
            conn, req.symbol, name=resolved_name, universe_tag="MANUAL_WATCHLIST",
        )
    finally:
        await conn.close()
    return {"status": "added", "symbol": req.symbol, "name": resolved_name}


@app.delete("/discovery/candidates/{symbol}")
async def remove_discovery_candidate(symbol: str):
    """후보에서 뺀다. 동적 후보라도 다음 갱신에서 다시 켜지지 않는다 (다시 넣으려면 수동 추가)."""
    conn = await db.get_connection()
    try:
        removed = await db.disable_candidate_symbol(conn, symbol)
    finally:
        await conn.close()
    if not removed:
        raise HTTPException(status_code=404, detail=f"활성 후보 중에 {symbol} 없음")
    return {"status": "removed", "symbol": symbol}


async def _run_discovery_refresh() -> None:
    conn = await db.get_connection()
    try:
        await discovery_sources.refresh_dynamic_universe(conn, engine.client)
    except Exception:
        logger.exception("수동 종목 발굴 갱신 실패")
    finally:
        await conn.close()


@app.post("/discovery/refresh")
async def refresh_discovery():
    """전종목 마스터 → 발굴 소스 → 후보 갱신 → 점수 재계산을 백그라운드로 시작한다. 모의투자에서는
    수 분 걸리므로 응답을 기다리지 않는다 - 진행 여부는 GET /discovery/shortlist의 refreshing으로 확인.
    (평소에는 스케줄러가 장 마감 후 하루 1회 자동으로 돈다)."""
    global _discovery_refresh_task
    if discovery_sources.is_refreshing():
        raise HTTPException(status_code=409, detail="종목 발굴 갱신이 이미 진행 중")
    _discovery_refresh_task = asyncio.create_task(_run_discovery_refresh())
    return {"status": "started"}


@app.get("/discovery/shortlist")
async def get_discovery_shortlist():
    """마지막 점수 캐시 기준 숏리스트 (현재 보유 종목 제외). KIS를 호출하지 않는다."""
    conn = await db.get_connection()
    try:
        cache = await discovery.load_scored_cache(conn)
        held = {row["symbol"] for row in await db.list_portfolio_symbols(conn)}
        enabled = {c["symbol"] for c in await db.list_candidate_universe(conn)}
        refreshed_at = await db.get_state(conn, discovery_sources.STATE_REFRESHED_AT)
        summary_raw = await db.get_state(conn, discovery_sources.STATE_REFRESH_SUMMARY)
    finally:
        await conn.close()
    entries = [e for e in (cache or {}).get("entries", []) if e["symbol"] in enabled]
    return {
        "refreshing": discovery_sources.is_refreshing(),
        "scored_at": (cache or {}).get("scored_at"),
        "refreshed_at": float(refreshed_at) if refreshed_at else None,
        "refresh_summary": json.loads(summary_raw) if summary_raw else None,
        "scored_count": len(entries),
        "shortlist": discovery_signals.select_with_quota(
            entries, settings.DISCOVERY_TOP_N, settings.DISCOVERY_ANGLE_QUOTA, exclude=held,
            min_score=settings.DISCOVERY_MIN_SCORE,
        ),
    }


@app.get("/ai-rebalance/scheduler")
async def get_scheduler_status():
    conn = await db.get_connection()
    try:
        enabled = (await db.get_state(conn, "ai_rebalance_scheduler_enabled")) == "1"
        auto_apply_state = await db.get_state(conn, "ai_rebalance_auto_apply")
        auto_apply = (auto_apply_state == "1") if auto_apply_state is not None else settings.AI_REBALANCE_AUTO_APPLY
        last_decision_at = await db.get_state(conn, "ai_rebalance_last_decision_at")
        last_scheduled_run_at = await db.get_state(conn, "ai_rebalance_last_scheduled_run_at")
    finally:
        await conn.close()
    return {
        "enabled": enabled,
        "auto_apply": auto_apply,
        "last_decision_at": float(last_decision_at) if last_decision_at else None,
        "last_scheduled_run_at": float(last_scheduled_run_at) if last_scheduled_run_at else None,
        "periodic_interval_days": settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS,
        "min_interval_sec": settings.AI_REBALANCE_MIN_INTERVAL_SEC,
    }


@app.post("/ai-rebalance/scheduler")
async def set_scheduler_enabled(req: SchedulerToggleRequest):
    """AI 포트폴리오 에이전트의 정기/드리프트/뉴스이벤트 트리거 루프 및 자동 반영(auto_apply)을 켜고 끈다."""
    conn = await db.get_connection()
    try:
        if req.enabled is not None:
            await db.set_state(conn, "ai_rebalance_scheduler_enabled", "1" if req.enabled else "0")
        if req.auto_apply is not None:
            await db.set_state(conn, "ai_rebalance_auto_apply", "1" if req.auto_apply else "0")
        enabled = (await db.get_state(conn, "ai_rebalance_scheduler_enabled")) == "1"
        auto_apply_state = await db.get_state(conn, "ai_rebalance_auto_apply")
        auto_apply = (auto_apply_state == "1") if auto_apply_state is not None else settings.AI_REBALANCE_AUTO_APPLY
    finally:
        await conn.close()
    return {"status": "ok", "enabled": enabled, "auto_apply": auto_apply}


class RunBacktestRequest(BaseModel):
    symbols: list[str] = ["005930", "000660", "017670"]
    start_date: str = "2025-01-01"
    end_date: str = "2026-09-30"
    initial_cash: float = 10_000_000.0
    benchmark_symbol: str = settings.BACKTEST_DEFAULT_BENCHMARK  # 360750
    benchmark_market: str = "domestic"
    benchmark_exchange: Optional[str] = None
    target_weights: Optional[dict[str, float]] = None
    fee_rate: float = settings.BACKTEST_DEFAULT_FEE_PCT
    tax_rate: float = settings.BACKTEST_DEFAULT_TAX_PCT
    slippage_pct: float = settings.BACKTEST_DEFAULT_SLIPPAGE_PCT
    stop_loss_pct: float = settings.STOP_LOSS_PCT


@app.post("/backtest/run")
async def run_backtest_endpoint(req: RunBacktestRequest):
    """지정 기간과 종목들에 대해 Finance Jarvis 트레이딩 전략(손절/ATR익절/지표시그널/온보딩)을
    과거 데이터로 시뮬레이션하고 동기간 S&P 500 ETF(기본: 360750) 벤치마크와 비교 검증합니다."""
    from core.backtest import BacktestConfig, BacktestRunner

    config = BacktestConfig(
        symbols=req.symbols,
        start_date=req.start_date,
        end_date=req.end_date,
        initial_cash=req.initial_cash,
        benchmark_symbol=req.benchmark_symbol,
        benchmark_market=req.benchmark_market,
        benchmark_exchange=req.benchmark_exchange,
        target_weights=req.target_weights,
        fee_rate=req.fee_rate,
        tax_rate=req.tax_rate,
        slippage_pct=req.slippage_pct,
        stop_loss_pct=req.stop_loss_pct,
    )
    conn = await db.get_connection()
    try:
        runner = BacktestRunner(conn, client=engine.client)
        return await runner.run(config)
    except Exception as e:
        logger.exception("백테스트 실행 실패")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()

