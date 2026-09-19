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

from core import db
from core.config import settings
from core.engine import TradingEngine
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


class SetWeightRequest(BaseModel):
    symbol: str
    weight: float


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


@app.post("/portfolio/symbols")
async def add_symbol(req: AddSymbolRequest):
    conn = await db.get_connection()
    try:
        await db.add_portfolio_symbol(conn, req.symbol, req.market)
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
