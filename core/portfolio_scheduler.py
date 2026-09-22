# core/portfolio_scheduler.py
"""AI 포트폴리오 에이전트의 백그라운드 트리거 루프.

TradingEngine과 완전히 독립적으로 동작한다 — 트레이딩 엔진이 꺼져 있어도(주말 리서치 등)
리밸런싱 제안 자체는 가능해야 하고, 서로 다른 장애 도메인(주/월 단위 LLM 호출 vs 30분
주문 실행 핫루프)을 한 루프에 섞지 않기 위함이다. `api/main.py`가 만든 공유
AsyncKISClient(engine.client)를 그대로 받아쓴다 — KIS는 토큰을 새로 발급할 때마다
카카오톡 알림을 보내므로, 별도 클라이언트를 새로 만들어 불필요한 토큰을 추가로
발급받지 않기 위함이다.

기본적으로 아무 것도 하지 않는다 - engine_state의 ai_rebalance_scheduler_enabled가
"1"일 때만 실제로 트리거를 검사한다 (기본값 꺼짐, 사용자가 명시적으로 켜야 함).
"""
import ast
import asyncio
import logging
import time
from typing import Optional

from core import db, kis_domestic, portfolio_agent, rebalancer
from core.config import settings
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SEC = 3600  # 거친 폴링 주기 - 실제 트리거는 날짜/임계값 기반이라 자주 볼 필요 없음


class PortfolioScheduler:
    def __init__(self, client: AsyncKISClient):
        self.client = client
        self._stopped = True
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while not self._stopped:
            try:
                await self._tick()
            except Exception:
                logger.exception("포트폴리오 스케줄러 tick 처리 중 예외 - 다음 tick으로 계속")
            try:
                await asyncio.sleep(CHECK_INTERVAL_SEC)
            except asyncio.CancelledError:
                return

    async def _tick(self) -> None:
        conn = await db.get_connection()
        try:
            if await db.get_state(conn, "ai_rebalance_scheduler_enabled") != "1":
                return
            if await self._cooldown_active(conn):
                return

            if await self._periodic_due(conn):
                logger.info("AI 포트폴리오 에이전트: 정기 재검토 트리거")
                await portfolio_agent.propose_rebalance(
                    conn, client=self.client, trigger_type="SCHEDULED",
                    trigger_detail=f"{settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS}일 정기 재검토",
                )
                await self._mark_decision(conn)
                return

            drift_detail = await self._drift_trigger_detail(conn)
            if drift_detail:
                logger.info(f"AI 포트폴리오 에이전트: 드리프트 트리거 - {drift_detail}")
                await portfolio_agent.propose_rebalance(
                    conn, client=self.client, trigger_type="DRIFT", trigger_detail=drift_detail,
                )
                await self._mark_decision(conn)
                return

            news_detail = await self._news_event_trigger_detail(conn)
            if news_detail:
                logger.info(f"AI 포트폴리오 에이전트: 뉴스이벤트 트리거 - {news_detail}")
                await portfolio_agent.propose_rebalance(
                    conn, client=self.client, trigger_type="NEWS_EVENT", trigger_detail=news_detail,
                )
                await self._mark_decision(conn)
        finally:
            await conn.close()

    async def _cooldown_active(self, conn) -> bool:
        raw = await db.get_state(conn, "ai_rebalance_last_decision_at")
        if raw is None:
            return False
        return (time.time() - float(raw)) < settings.AI_REBALANCE_MIN_INTERVAL_SEC

    async def _mark_decision(self, conn) -> None:
        await db.set_state(conn, "ai_rebalance_last_decision_at", str(time.time()))

    async def _periodic_due(self, conn) -> bool:
        raw = await db.get_state(conn, "ai_rebalance_last_scheduled_run_at")
        if raw is None:
            due = True
        else:
            elapsed_days = (time.time() - float(raw)) / 86400
            due = elapsed_days >= settings.AI_REBALANCE_PERIODIC_INTERVAL_DAYS
        if due:
            await db.set_state(conn, "ai_rebalance_last_scheduled_run_at", str(time.time()))
        return due

    async def _drift_trigger_detail(self, conn) -> Optional[str]:
        """국내 종목만 검사한다 - 해외는 환율까지 조회해야 해 이 저빈도 체크의 범위를
        벗어난다 (해외 종목의 드리프트는 정기(SCHEDULED) 트리거가 대신 커버한다)."""
        active_weights = await db.get_active_target_weights(conn)
        if not active_weights:
            return None
        symbol_info = await db.get_portfolio_symbol_info(conn)
        positions = await db.get_positions(conn)
        domestic_symbols = [
            s for s in active_weights
            if (symbol_info.get(s) or {}).get("market", "domestic") == "domestic"
        ]
        if not domestic_symbols:
            return None

        try:
            balance = await kis_domestic.get_balance(self.client)
        except Exception:
            logger.exception("드리프트 트리거 확인 중 잔고조회 실패 - 이번 tick은 건너뜀")
            return None
        total_equity = float(balance["summary"].get("nass_amt") or 0)
        if total_equity <= 0:
            return None

        for symbol in domestic_symbols:
            try:
                price_info = await kis_domestic.get_price(self.client, symbol)
                price = float(price_info.get("stck_prpr") or 0)
            except Exception:
                continue
            if price <= 0:
                continue
            qty = float((positions.get(symbol) or {}).get("qty") or 0)
            current_weight = (qty * price) / total_equity
            target = active_weights[symbol]
            drift = rebalancer.compute_drift(current_weight, target, settings.REBALANCE_BAND_PCT)
            if not drift.in_band and abs(drift.drift) > (
                settings.REBALANCE_BAND_PCT + settings.AI_REBALANCE_DRIFT_TRIGGER_BUFFER_PCT
            ):
                return f"{symbol} 드리프트 {drift.drift:+.1%} (목표 {target:.1%}, 현재 {current_weight:.1%})"
        return None

    async def _news_event_trigger_detail(self, conn) -> Optional[str]:
        """TradingEngine이 30분 사이클마다 이미 계산해 decision_log에 남긴 뉴스감성을
        재사용한다 - 별도 뉴스 폴링 인프라를 새로 만들지 않는다."""
        active_weights = await db.get_active_target_weights(conn)
        if not active_weights:
            return None
        raw = await db.get_state(conn, "ai_rebalance_last_decision_at")
        since_ts = float(raw) if raw is not None else (time.time() - 86400)

        cur = await conn.execute(
            "SELECT symbol, sentiment_signal FROM decision_log WHERE ts > ? "
            "AND sentiment_signal IS NOT NULL AND sentiment_signal != 'None' "
            "ORDER BY id DESC LIMIT 200",
            (since_ts,),
        )
        rows = await cur.fetchall()
        for row in rows:
            if row["symbol"] not in active_weights:
                continue
            try:
                parsed = ast.literal_eval(row["sentiment_signal"])
                strength = float(parsed.get("strength") or 0)
                direction = parsed.get("direction")
            except Exception:
                continue
            if strength >= settings.AI_REBALANCE_NEWS_TRIGGER_STRENGTH:
                return f"{row['symbol']} 뉴스감성 강도 {strength:.2f} ({direction})"
        return None
