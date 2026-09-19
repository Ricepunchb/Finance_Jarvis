# core/engine.py
"""자동매매 오케스트레이터. 종목별 처리는 서로 격리되어 하나가 실패해도 나머지는 계속된다.

Phase 1 범위: 국내주식만, 목표비중은 수동으로 승인된 active_target_weights만 사용,
뉴스/LLM 시그널은 아직 없음(sentiment_signal=None). 시장시간 인지는 KRX 정규장
(09:00~15:30 KST, 평일)만 고려하고 휴장일 캘린더는 Phase 3 이후로 미룬다.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db, indicators, kis_domestic, reconciliation, signal_engine
from core.config import settings
from core.kis_client import AsyncKISClient
from core.kis_domestic import KisApiError
from core.lock import InstanceLock
from core.risk import RiskManager
from core.websocket_client import KISWebSocketClient

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
CYCLE_INTERVAL_SEC = 60
CHART_LOOKBACK_DAYS = 90


def _is_krx_open_now() -> bool:
    now = datetime.now(tz=KST)
    if now.weekday() >= 5:  # 5=토, 6=일
        return False
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


class TradingEngine:
    def __init__(self):
        self.client = AsyncKISClient()
        self.conn: Optional[aiosqlite.Connection] = None
        self.risk: Optional[RiskManager] = None
        self.lock = InstanceLock(settings.ENGINE_LOCK_PATH)
        self.ws_client: Optional[KISWebSocketClient] = None
        self._stop_event = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        settings.assert_trading_allowed()
        self.lock.acquire()  # 실패하면 EngineAlreadyRunningError -> 즉시 상위로 전파

        await db.init_db()
        self.conn = await db.get_connection()
        self.risk = RiskManager(self.conn)

        logger.info("시작 전 대조(reconciliation) 실행 중 — 완료 전까지 신규주문 없음")
        await reconciliation.run_startup_reconciliation(self.client, self.conn)

        await db.set_state(self.conn, "engine_running", "1")
        await db.set_state(self.conn, "pid", str(os.getpid()))
        await db.set_state(self.conn, "lock_acquired_at", str(time.time()))

        self.ws_client = KISWebSocketClient(
            hts_id=settings.KIS_HTS_ID, on_fill=self._on_fill, on_any_message=self._on_any_ws_message
        )
        await self.ws_client.start()

        self._stop_event.clear()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """정상 정지: 신규 사이클을 멈추지만 이미 체결된 포지션/미체결 주문은 그대로 둔다."""
        self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
        if self.ws_client is not None:
            await self.ws_client.stop()
        if self.conn is not None:
            await db.set_state(self.conn, "engine_running", "0")
            await self.conn.close()
        self.lock.release()
        await self.client.close()

    async def kill(self) -> None:
        """비상정지: kill switch를 트립하고 미체결 주문을 전량취소한 뒤 정상 정지한다."""
        if self.risk is not None:
            await self.risk.trip_kill_switch("사용자 긴급정지 버튼")
        try:
            cancelable = await kis_domestic.get_cancelable_orders(self.client)
            for row in cancelable:
                psbl_qty = float(row.get("psbl_qty") or 0)
                if psbl_qty <= 0:
                    continue
                await kis_domestic.cancel_order(
                    self.client, row.get("pdno"), row.get("ord_gno_brno"), row.get("odno")
                )
        except Exception:
            logger.exception("긴급정지 중 미체결주문 취소 실패 — 수동 확인 필요")
        await self.stop()

    async def _on_any_ws_message(self) -> None:
        if self.risk is not None:
            await self.risk.mark_ws_message_received()

    async def _on_fill(self, row: dict) -> None:
        """체결통보 수신 -> 해당 order_intent를 찾아 상태 갱신 + fills 기록."""
        if self.conn is None:
            return
        kis_order_no = row.get("ODER_NO")
        cntg_yn = row.get("CNTG_YN")
        rfus_yn = row.get("RFUS_YN")
        if not kis_order_no:
            return

        cur = await self.conn.execute(
            "SELECT intent_id, qty FROM order_intents WHERE kis_order_no = ?", (kis_order_no,)
        )
        intent = await cur.fetchone()
        if intent is None:
            return  # 우리가 낸 주문이 아니거나 아직 SUBMITTED로 기록 전 (드문 레이스, 다음 대조에서 정리됨)

        if rfus_yn == "Y":
            await db.update_order_intent(self.conn, intent["intent_id"], status="REJECTED")
            return

        if cntg_yn == "2":  # 실제 체결
            try:
                cntg_qty = float(row.get("CNTG_QTY") or 0)
                cntg_price = float(row.get("CNTG_UNPR") or 0)
            except ValueError:
                cntg_qty, cntg_price = 0.0, 0.0
            await self.conn.execute(
                "INSERT INTO fills(intent_id, qty, price, filled_at, source) VALUES (?, ?, ?, ?, 'WS')",
                (intent["intent_id"], cntg_qty, cntg_price, time.time()),
            )
            await self.conn.commit()
            status = "FILLED" if cntg_qty >= float(intent["qty"]) else "PARTIALLY_FILLED"
            await db.update_order_intent(self.conn, intent["intent_id"], status=status)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._run_one_cycle()
            except Exception:
                logger.exception("사이클 실행 중 처리되지 않은 예외 — 다음 사이클로 계속")
            if self.conn is not None:
                await db.set_state(self.conn, "heartbeat_at", str(time.time()))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=CYCLE_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _run_one_cycle(self) -> None:
        assert self.conn is not None and self.risk is not None
        if not _is_krx_open_now():
            return
        if await self.risk.is_kill_switch_active():
            return

        active_weights = await db.get_active_target_weights(self.conn)
        if not active_weights:
            return

        cycle_id = await db.new_cycle(self.conn)
        ws_ok = await self.risk.is_ws_healthy()

        balance = await kis_domestic.get_balance(self.client)
        holdings_by_symbol = {h["pdno"]: h for h in balance["holdings"] if h.get("pdno")}
        total_equity = float(balance["summary"].get("nass_amt") or 0)
        for symbol, holding in holdings_by_symbol.items():
            await db.upsert_position(
                self.conn, symbol, float(holding.get("hldg_qty") or 0), float(holding.get("pchs_avg_pric") or 0)
            )

        halted_count = 0
        for symbol in active_weights:
            try:
                halted = await self._process_symbol(
                    cycle_id, symbol, active_weights[symbol], holdings_by_symbol.get(symbol),
                    total_equity, ws_ok,
                )
                if halted:
                    halted_count += 1
            except Exception:
                logger.exception(f"'{symbol}' 처리 중 예외 — 이 종목만 건너뛰고 계속")

        await self.risk.check_market_wide_halt_breadth(halted_count, len(active_weights))

    async def _log_no_op(self, cycle_id: int, symbol: str, target_weight: float, reason: str) -> None:
        await db.log_decision(
            self.conn, cycle_id, symbol, None, target_weight, None, "N/A", "N/A", "NO_OP", None, reason
        )

    async def _process_symbol(
        self, cycle_id: int, symbol: str, target_weight: float, holding: Optional[dict],
        total_equity: float, ws_ok: bool,
    ) -> bool:
        """반환값: 이 종목이 VI/거래정지 상태였는지 (시장전체 이상 감지용)."""
        assert self.conn is not None and self.risk is not None

        if not ws_ok:
            await self._log_no_op(cycle_id, symbol, target_weight, "체결통보 웹소켓 비정상 - 신규주문 차단")
            return False
        if await self.risk.is_cooldown_active(symbol):
            await self._log_no_op(cycle_id, symbol, target_weight, "쿨다운 중")
            return False
        if await self.risk.is_daily_loss_limit_hit(total_equity):
            await self._log_no_op(cycle_id, symbol, target_weight, "일일 손실 한도 도달")
            return False

        vi_rows = await kis_domestic.get_vi_status(self.client, symbol)
        if vi_rows:
            await self._log_no_op(cycle_id, symbol, target_weight, "VI 발동 중 - 매매 보류")
            return True

        price_info = await kis_domestic.get_price(self.client, symbol)
        price = float(price_info.get("stck_prpr") or 0)
        if price <= 0:
            await self._log_no_op(cycle_id, symbol, target_weight, "현재가 조회 실패")
            return False

        qty = float(holding.get("hldg_qty") or 0) if holding else 0.0
        position_value = qty * price
        current_weight = (position_value / total_equity) if total_equity > 0 else 0.0

        today = datetime.now(tz=KST)
        start_date = (today - timedelta(days=CHART_LOOKBACK_DAYS)).strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")
        chart_rows = await kis_domestic.get_daily_chart(self.client, symbol, start_date, end_date)
        df = indicators.chart_rows_to_dataframe(chart_rows)
        tech_signal = indicators.compute_technical_signal(df)

        action = await signal_engine.decide(
            current_weight=current_weight,
            target_weight=target_weight,
            band=settings.REBALANCE_BAND_PCT,
            tech_signal=tech_signal,
            sentiment_signal=None,  # Phase 2에서 LLM 뉴스 시그널 연결
            price=price,
            current_position_qty=qty,
            current_position_value=position_value,
            total_equity=total_equity,
            risk=self.risk,
        )

        if action is None:
            await self._log_no_op(cycle_id, symbol, target_weight, f"조건 미충족 (tech={tech_signal})")
            return False

        # 주문 직전 항상 실시간으로 재확인 — 캐시/이전 조회 신뢰 금지.
        if action.side == "buy":
            psbl = await kis_domestic.get_buyable_cash(self.client, symbol, int(price))
            action.qty = min(action.qty, float(psbl.get("max_buy_qty") or 0))
        else:
            # 매도가능수량조회(inquire-psbl-sell)는 모의투자에서 "EGW02006 모의투자 TR이
            # 아닙니다"로 거부되는 것을 실제로 확인했다 — 모의투자에서는 이번 사이클 시작에
            # 조회한 보유수량을 안전한(과대추정 없는) 상한으로 대신 사용한다.
            if settings.IS_MOCK:
                action.qty = min(action.qty, qty)
            else:
                psbl = await kis_domestic.get_sellable_qty(self.client, symbol)
                action.qty = min(action.qty, float(psbl.get("ord_psbl_qty") or 0))
        action.qty = float(int(action.qty))

        if action.qty <= 0:
            await self._log_no_op(cycle_id, symbol, target_weight, "주문가능수량 0")
            return False

        try:
            intent_id = await db.create_order_intent(
                self.conn, cycle_id, symbol, action.side, action.qty, action.order_type,
                action.price, action.reason,
            )
        except aiosqlite.IntegrityError:
            # (symbol, cycle_id) UNIQUE 위반 — 이번 사이클에 이미 이 종목 intent 존재. 이중주문 방지.
            logger.warning(f"'{symbol}' 이번 사이클에 이미 intent 존재 — 주문 스킵")
            return False

        try:
            result = await kis_domestic.order_cash(
                self.client, symbol, action.side, int(action.qty),
                int(action.price) if action.price else None,
            )
            kis_order_no = result.get("ODNO") or result.get("odno")
            await db.update_order_intent(self.conn, intent_id, status="SUBMITTED", kis_order_no=kis_order_no)
        except KisApiError as e:
            await db.update_order_intent(self.conn, intent_id, status="REJECTED")
            await self.risk.record_order_rejection()
            await db.log_decision(
                self.conn, cycle_id, symbol, current_weight, target_weight,
                current_weight - target_weight, tech_signal["direction"], "N/A",
                action.side.upper(), action.qty, f"주문 거부: {e.msg1}", intent_id,
            )
            return False

        await db.log_decision(
            self.conn, cycle_id, symbol, current_weight, target_weight,
            current_weight - target_weight, tech_signal["direction"], "N/A",
            action.side.upper(), action.qty, action.reason, intent_id,
        )
        return False
