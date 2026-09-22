# api/main.py
"""자동매매 엔진 제어 플레인. 엔진은 이 프로세스 안에서 /engine/start 호출 시에만
백그라운드 asyncio 태스크로 기동한다 (프로세스 부팅 시 자동시작 안 함).

실행: uvicorn api.main:app --port 8800
"""
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from core import db, signal_engine
from core.config import settings
from core.engine import CYCLE_INTERVAL_SEC, TradingEngine
from core.lock import EngineAlreadyRunningError
from core.risk import RiskManager

engine = TradingEngine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


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
        "cycle_interval_sec": CYCLE_INTERVAL_SEC,
        "rebalance_band_pct": settings.REBALANCE_BAND_PCT,
        "max_position_pct": settings.MAX_POSITION_PCT,
        "max_order_notional_krw": settings.MAX_ORDER_NOTIONAL_KRW,
        "max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT,
        "order_cooldown_sec": settings.ORDER_COOLDOWN_SEC,
        "ws_staleness_threshold_sec": settings.WS_STALENESS_THRESHOLD_SEC,
        "llm_provider": settings.LLM_PROVIDER,
        "gemini_model": settings.GEMINI_MODEL,
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
    }


@app.post("/portfolio/symbols")
async def add_symbol(req: AddSymbolRequest):
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
    """LLM(Gemini)에게 현재 등록된 전체 종목의 목표비중 초안을 실제로 요청한다.

    응답은 PROPOSED 상태로만 저장되고, 사람이 /portfolio/weights/proposals/{id}/decide로
    승인해야만 active_target_weights에 반영되어 실제 매매에 쓰인다.
    """
    if not settings.GEMINI_API_KEY:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY가 설정되지 않았습니다.")

    from core.llm.factory import get_llm_provider

    conn = await db.get_connection()
    try:
        symbols_rows = await db.list_portfolio_symbols(conn)
        symbols = [row["symbol"] for row in symbols_rows]
        if not symbols:
            raise HTTPException(status_code=400, detail="등록된 종목이 없습니다.")

        provider = get_llm_provider()
        result = await provider.propose_weights(symbols)

        proposal_ids = []
        for symbol, weight in result["weights"].items():
            proposal_id = await db.propose_target_weight(
                conn, symbol, weight, proposed_by="llm", rationale=result.get("rationale", "")
            )
            proposal_ids.append(proposal_id)
        return {"status": "proposed", "proposal_ids": proposal_ids, "rationale": result.get("rationale", "")}
    finally:
        await conn.close()


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


@app.get("/portfolio/positions")
async def get_positions():
    conn = await db.get_connection()
    try:
        return await db.get_positions(conn)
    finally:
        await conn.close()


@app.get("/decisions/recent")
async def recent_decisions(limit: int = 50):
    conn = await db.get_connection()
    try:
        return await db.get_recent_decisions(conn, limit)
    finally:
        await conn.close()
