# core/engine.py
"""자동매매 오케스트레이터. 종목별 처리는 서로 격리되어 하나가 실패해도 나머지는 계속된다.

Phase 1: 국내주식만, 목표비중은 승인된 active_target_weights만 사용.
Phase 2: 뉴스 기반 LLM 감성 시그널 추가 — GEMINI_API_KEY가 없으면 자동으로 비활성화되어
Phase 1 방식(기술적 지표만)으로 그대로 동작한다.
Phase 3: 해외주식(미국 NASD/NYSE/AMEX만 실증됨) 추가. 국내/해외는 서로 다른 시장시간을
가지므로 사이클 게이트는 "둘 중 하나라도 열려있으면 진행"으로 바뀌고, 각 종목은 자신의
시장이 열려있을 때만 실제로 처리된다. 휴장일 캘린더는 아직 없음(국내/미국 모두).
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db, exit_guard, indicators, intraday, kis_domestic, kis_overseas, reconciliation, signal_engine
from core.config import settings
from core.fundamentals import valuation as fundamental_valuation
from core.kis_client import AsyncKISClient
from core.kis_common import KisApiError
from core.llm.base import LLMProvider
from core.lock import InstanceLock
from core.news import sentiment as news_sentiment
from core.risk import RiskManager
from core.websocket_client import KISWebSocketClient

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
CYCLE_INTERVAL_SEC = 1800  # 종목당 모니터링/매매 윈도우: 30분
CHART_LOOKBACK_DAYS = 90

# 미국 거래소(NASD/NYSE/AMEX) 정규장. 정확한 DST 전환일은 아직 반영하지 않고
# 월(3~11월 서머타임/12~2월 표준시)로만 근사한다 — 국내 KRX 휴장일 캘린더와
# 마찬가지로 Phase 3에서도 의도적으로 미룬 부분.
_US_EXCHANGES = {"NASD", "NYSE", "AMEX"}


def _is_krx_open_now() -> bool:
    now = datetime.now(tz=KST)
    if now.weekday() >= 5:  # 5=토, 6=일
        return False
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def _is_us_market_open_now() -> bool:
    now = datetime.now(tz=KST)
    is_dst = 3 <= now.month <= 11
    open_hour, close_hour = (22, 5) if is_dst else (23, 6)
    # 야간장이라 날짜를 걸치므로(예: 23:30~06:00) 요일 판정은 개장 시각 기준으로 한다.
    if now.hour >= open_hour:
        weekday = now.weekday()  # 이날 밤에 열리는 장 (금요일 밤은 열림, 토요일 밤은 안 열림)
        return weekday <= 4
    if now.hour < close_hour:
        weekday = (now.weekday() - 1) % 7  # 전날 밤 장이 아직 진행 중인 새벽 시간
        return weekday <= 4
    return False


def _is_market_open(market: str, exchange: Optional[str]) -> bool:
    if market == "overseas" and exchange in _US_EXCHANGES:
        return _is_us_market_open_now()
    return _is_krx_open_now()


class TradingEngine:
    def __init__(self):
        self.client = AsyncKISClient()
        self.conn: Optional[aiosqlite.Connection] = None
        self.risk: Optional[RiskManager] = None
        self.lock = InstanceLock(settings.ENGINE_LOCK_PATH)
        self.ws_client: Optional[KISWebSocketClient] = None
        self.llm_provider: Optional[LLMProvider] = None
        self._stop_event = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None
        self._intraday_backfill_budget = 0  # 사이클마다 리셋 — 모의투자 1req/sec 제약 때문에 분봉 백필을 종목 몇 개로 제한

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

        if settings.GEMINI_API_KEY:
            from core.llm.factory import get_llm_provider
            try:
                self.llm_provider = get_llm_provider()
            except Exception:
                logger.exception("LLM provider 초기화 실패 - 이번 실행은 기술적 지표만으로 동작")
                self.llm_provider = None
        else:
            logger.info("GEMINI_API_KEY 미설정 - 뉴스 감성분석 비활성화, 기술적 지표만 사용")

        self.ws_client = KISWebSocketClient(
            hts_id=settings.KIS_HTS_ID, on_fill=self._on_fill, on_any_message=self._on_any_ws_message,
            include_overseas=True,
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
        """비상정지: kill switch를 트립하고 미체결 주문(국내+해외)을 전량취소한 뒤 정상 정지한다."""
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
            logger.exception("긴급정지 중 국내 미체결주문 취소 실패 — 수동 확인 필요")

        try:
            today = datetime.now(tz=KST).strftime("%Y%m%d")
            ccnl_rows = await kis_overseas.get_ccnl(self.client, start_date=today, end_date=today)
            for row in ccnl_rows:
                nccs_qty = float(row.get("nccs_qty") or 0)  # 미체결수량
                if nccs_qty <= 0:
                    continue
                await kis_overseas.cancel_order(
                    self.client, row.get("ovrs_excg_cd"), row.get("pdno"), row.get("odno"),
                    int(nccs_qty), float(row.get("ft_ord_unpr3") or 0),
                )
        except Exception:
            logger.exception("긴급정지 중 해외 미체결주문 취소 실패 — 수동 확인 필요")
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
        if not (_is_krx_open_now() or _is_us_market_open_now()):
            return  # 국내/미국 둘 다 닫혀있으면 이번 사이클은 아예 건너뛴다 (불필요한 API 호출 방지)
        if await self.risk.is_kill_switch_active():
            return

        active_weights = await db.get_active_target_weights(self.conn)
        if not active_weights:
            return

        symbol_info = await db.get_portfolio_symbol_info(self.conn)
        cycle_id = await db.new_cycle(self.conn)
        ws_ok = await self.risk.is_ws_healthy()
        self._intraday_backfill_budget = settings.INTRADAY_BACKFILL_SYMBOLS_PER_CYCLE

        balance = await kis_domestic.get_balance(self.client)
        holdings_by_symbol = {h["pdno"]: h for h in balance["holdings"] if h.get("pdno")}
        total_equity = float(balance["summary"].get("nass_amt") or 0)
        for symbol, holding in holdings_by_symbol.items():
            await db.upsert_position(
                self.conn, symbol, float(holding.get("hldg_qty") or 0), float(holding.get("pchs_avg_pric") or 0)
            )

        # 관리 중인 종목 중 해외가 하나라도 있을 때만 해외 잔고를 조회한다 (국내전용 사용자는
        # Phase 1/2 때와 동일하게 이 호출이 아예 발생하지 않는다).
        overseas_holdings_by_symbol: dict = {}
        if any(symbol_info.get(s, {}).get("market") == "overseas" for s in active_weights):
            try:
                present = await kis_overseas.get_present_balance_krw(self.client)
                overseas_holdings_by_symbol = {h["pdno"]: h for h in present["holdings"] if h.get("pdno")}
                total_equity += float((present["totals"] or {}).get("tot_asst_amt") or 0)
                for symbol, holding in overseas_holdings_by_symbol.items():
                    await db.upsert_position(
                        self.conn, symbol, float(holding.get("cblc_qty13") or 0),
                        float(holding.get("avg_unpr3") or 0), currency=holding.get("crcy_cd") or "USD",
                    )
            except Exception:
                logger.exception("해외 잔고조회 실패 - 이번 사이클은 해외 종목 처리를 건너뜀")

        halted_count = 0
        domestic_symbol_count = 0
        for symbol in active_weights:
            info = symbol_info.get(symbol) or {"market": "domestic", "exchange": None}
            market = info.get("market") or "domestic"
            exchange = info.get("exchange")

            if not _is_market_open(market, exchange):
                continue  # 이 종목이 속한 시장이 지금 닫혀있음 - 매 사이클 로그가 쌓이지 않게 조용히 스킵

            try:
                if market == "overseas":
                    await self._process_overseas_symbol(
                        cycle_id, symbol, exchange, active_weights[symbol],
                        overseas_holdings_by_symbol.get(symbol), total_equity, ws_ok,
                    )
                else:
                    domestic_symbol_count += 1
                    halted = await self._process_symbol(
                        cycle_id, symbol, active_weights[symbol], holdings_by_symbol.get(symbol),
                        total_equity, ws_ok,
                    )
                    if halted:
                        halted_count += 1
            except Exception:
                logger.exception(f"'{symbol}' 처리 중 예외 — 이 종목만 건너뛰고 계속")

        if domestic_symbol_count > 0:
            # 해외 종목은 VI 개념이 없어 halted 판정에 안 들어가므로, 분모를 국내 종목 수로만 잡는다.
            await self.risk.check_market_wide_halt_breadth(halted_count, domestic_symbol_count)

    async def _log_no_op(
        self, cycle_id: int, symbol: str, target_weight: float, reason: str,
        context: Optional[dict] = None,
    ) -> None:
        await db.log_decision(
            self.conn, cycle_id, symbol, None, target_weight, None, "N/A", "N/A", "NO_OP", None,
            reason, context=context,
        )

    async def _submit_exit_order(
        self, cycle_id: int, symbol: str, target_weight: float, current_weight: float,
        qty: float, price: float, avg_price: float, exit_reason: str, context: dict,
    ) -> None:
        """국내 손절/트레일링익절 강제매도. 체결 확실성이 우선이므로 시장가로 낸다
        (order_cash는 price=None이면 시장가 — 기존에 실사용 이력이 없던 경로라 모의투자로
        먼저 검증할 것). 쿨다운/일일손실한도를 의도적으로 우회하지만, (symbol, cycle_id)
        UNIQUE 제약은 그대로 적용되어 같은 사이클 이중주문은 여전히 불가능하다."""
        qty = float(int(qty))
        try:
            intent_id = await db.create_order_intent(
                self.conn, cycle_id, symbol, "sell", qty, "market", None, exit_reason,
            )
        except aiosqlite.IntegrityError:
            logger.warning(f"'{symbol}' 이번 사이클에 이미 intent 존재 — 손절/익절 주문 스킵")
            return

        try:
            result = await kis_domestic.order_cash(self.client, symbol, "sell", int(qty), None)
            kis_order_no = result.get("ODNO") or result.get("odno")
            await db.update_order_intent(self.conn, intent_id, status="SUBMITTED", kis_order_no=kis_order_no)
        except KisApiError as e:
            await db.update_order_intent(self.conn, intent_id, status="REJECTED")
            await self.risk.record_order_rejection()
            await db.log_decision(
                self.conn, cycle_id, symbol, current_weight, target_weight, current_weight - target_weight,
                "N/A", "N/A", "SELL", qty, f"{exit_reason} 주문 거부: {e.msg1}", intent_id, context=context,
            )
            return

        await self.risk.add_daily_pnl((price - avg_price) * qty)
        await db.log_decision(
            self.conn, cycle_id, symbol, current_weight, target_weight, current_weight - target_weight,
            "N/A", "N/A", "SELL", qty, exit_reason, intent_id, context=context,
        )

    async def _process_symbol(
        self, cycle_id: int, symbol: str, target_weight: float, holding: Optional[dict],
        total_equity: float, ws_ok: bool,
    ) -> bool:
        """반환값: 이 종목이 VI/거래정지 상태였는지 (시장전체 이상 감지용)."""
        assert self.conn is not None and self.risk is not None

        # 사후 재현/디버깅용 원시 스냅샷 — 계산해가는 값들을 그때그때 채워 어느 지점에서
        # 멈췄든 log_decision에 그대로 넘긴다 (지금 안 남기면 그 사이클의 판단 근거는 영구 소실됨).
        context: dict = {"target_weight": target_weight, "total_equity": total_equity}

        if not ws_ok:
            await self._log_no_op(cycle_id, symbol, target_weight, "체결통보 웹소켓 비정상 - 신규주문 차단", context)
            return False

        vi_rows = await kis_domestic.get_vi_status(self.client, symbol)
        if vi_rows:
            context["vi_rows"] = vi_rows
            await self._log_no_op(cycle_id, symbol, target_weight, "VI 발동 중 - 매매 보류", context)
            return True

        price_info = await kis_domestic.get_price(self.client, symbol)
        price = float(price_info.get("stck_prpr") or 0)
        context["price"] = price
        if price <= 0:
            await self._log_no_op(cycle_id, symbol, target_weight, "현재가 조회 실패", context)
            return False

        qty = float(holding.get("hldg_qty") or 0) if holding else 0.0
        position_value = qty * price
        current_weight = (position_value / total_equity) if total_equity > 0 else 0.0
        context.update({"qty_before": qty, "position_value": position_value, "current_weight": current_weight})

        # --- 손절/트레일링익절: 쿨다운/일일손실한도보다 먼저, 그리고 그것들과 무관하게 평가한다.
        # 이 두 게이트는 "새로운 재량매매"를 막기 위한 것이지 손실을 줄이는 강제청산을 막기
        # 위한 게 아니다 — 킬스위치와 같은 급의 예외로 취급한다.
        if qty > 0:
            avg_price = float(holding.get("pchs_avg_pric") or 0) if holding else 0.0
            await db.update_position_peak(self.conn, symbol, price)
            position_row = await db.get_position(self.conn, symbol)
            peak_price = position_row["peak_price_since_entry"] if position_row else None
            exit_decision = exit_guard.evaluate_exit(avg_price, peak_price, price)
            context["exit_check"] = {
                "avg_price": avg_price, "peak_price": peak_price,
                "fired": exit_decision.reason if exit_decision else None,
            }
            if exit_decision is not None:
                await self._submit_exit_order(
                    cycle_id, symbol, target_weight, current_weight, qty, price, avg_price,
                    exit_decision.reason, context,
                )
                return False

        if await self.risk.is_cooldown_active(symbol):
            await self._log_no_op(cycle_id, symbol, target_weight, "쿨다운 중", context)
            return False
        if await self.risk.is_daily_loss_limit_hit(total_equity):
            await self._log_no_op(cycle_id, symbol, target_weight, "일일 손실 한도 도달", context)
            return False

        today = datetime.now(tz=KST)
        start_date = (today - timedelta(days=CHART_LOOKBACK_DAYS)).strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")
        chart_rows = await kis_domestic.get_daily_chart(self.client, symbol, start_date, end_date)
        df = indicators.chart_rows_to_dataframe(chart_rows)
        tech_signal = indicators.compute_technical_signal(df)
        context["tech_signal"] = tech_signal

        allow_backfill = self._intraday_backfill_budget > 0
        self._intraday_backfill_budget -= 1
        intraday_signal = await intraday.get_intraday_signal_domestic(self.client, self.conn, symbol, allow_backfill)
        context["intraday_signal"] = intraday_signal

        sentiment_signal = None
        if self.llm_provider is not None:
            try:
                sentiment_signal = await news_sentiment.get_symbol_sentiment(
                    self.conn, self.llm_provider, symbol
                )
            except Exception:
                # 뉴스/LLM 파이프라인 장애가 매매 로직 전체를 막으면 안 된다 - 기술적 지표만으로 계속.
                logger.exception(f"'{symbol}' 뉴스 감성분석 파이프라인 오류 - 기술적 지표만 사용")
        context["sentiment_signal"] = sentiment_signal

        valuation_signal = None
        if settings.ENABLE_FUNDAMENTAL_VALUATION:
            try:
                valuation_signal = await fundamental_valuation.get_valuation_signal(self.conn, self.client, symbol)
            except Exception:
                logger.exception(f"'{symbol}' 밸류에이션 시그널 조회 오류 - 밸류에이션 없이 계속")
        context["valuation_signal"] = valuation_signal

        action = await signal_engine.decide(
            current_weight=current_weight,
            target_weight=target_weight,
            band=settings.REBALANCE_BAND_PCT,
            tech_signal=tech_signal,
            intraday_signal=intraday_signal,
            sentiment_signal=sentiment_signal,
            valuation_signal=valuation_signal,
            price=price,
            current_position_qty=qty,
            current_position_value=position_value,
            total_equity=total_equity,
            risk=self.risk,
        )

        if action is None:
            await self._log_no_op(
                cycle_id, symbol, target_weight,
                f"조건 미충족 (tech={tech_signal}, intraday={intraday_signal}, "
                f"sentiment={sentiment_signal}, valuation={valuation_signal})",
                context,
            )
            return False

        context["action_before_recheck"] = {"side": action.side, "qty": action.qty, "reason": action.reason}

        # 주문 직전 항상 실시간으로 재확인 — 캐시/이전 조회 신뢰 금지.
        if action.side == "buy":
            psbl = await kis_domestic.get_buyable_cash(self.client, symbol, int(price))
            context["buyable_check"] = psbl
            action.qty = min(action.qty, float(psbl.get("max_buy_qty") or 0))
        else:
            # 매도가능수량조회(inquire-psbl-sell)는 모의투자에서 "EGW02006 모의투자 TR이
            # 아닙니다"로 거부되는 것을 실제로 확인했다 — 모의투자에서는 이번 사이클 시작에
            # 조회한 보유수량을 안전한(과대추정 없는) 상한으로 대신 사용한다.
            if settings.IS_MOCK:
                action.qty = min(action.qty, qty)
                context["sellable_check"] = {"mock_fallback_to_held_qty": qty}
            else:
                psbl = await kis_domestic.get_sellable_qty(self.client, symbol)
                context["sellable_check"] = psbl
                action.qty = min(action.qty, float(psbl.get("ord_psbl_qty") or 0))
        action.qty = float(int(action.qty))
        context["qty_after_recheck"] = action.qty

        if action.qty <= 0:
            await self._log_no_op(cycle_id, symbol, target_weight, "주문가능수량 0", context)
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
                current_weight - target_weight, tech_signal["direction"], str(sentiment_signal),
                action.side.upper(), action.qty, f"주문 거부: {e.msg1}", intent_id,
                context=context,
            )
            return False

        await db.log_decision(
            self.conn, cycle_id, symbol, current_weight, target_weight,
            current_weight - target_weight, tech_signal["direction"], str(sentiment_signal),
            action.side.upper(), action.qty, action.reason, intent_id,
            context=context,
        )
        return False

    async def _submit_exit_order_overseas(
        self, cycle_id: int, symbol: str, exchange: str, target_weight: float, current_weight: float,
        qty: float, price_foreign: float, price_krw: float, avg_price_krw: float,
        exit_reason: str, context: dict,
    ) -> None:
        """해외 손절/트레일링익절 강제매도. 해외는 시장가를 지원하지 않으므로(FX 리스크로
        의도적 배제) 직전가 대비 2% 할인된 지정가로 체결확률을 확보한다."""
        qty = float(int(qty))
        discounted_price = round(price_foreign * 0.98, 2)
        try:
            intent_id = await db.create_order_intent(
                self.conn, cycle_id, symbol, "sell", qty, "limit", discounted_price, exit_reason,
                market="overseas",
            )
        except aiosqlite.IntegrityError:
            logger.warning(f"'{symbol}' 이번 사이클에 이미 intent 존재 — 손절/익절 주문 스킵")
            return

        try:
            result = await kis_overseas.order(self.client, exchange, symbol, "sell", int(qty), discounted_price)
            kis_order_no = result.get("ODNO") or result.get("odno")
            await db.update_order_intent(self.conn, intent_id, status="SUBMITTED", kis_order_no=kis_order_no)
        except KisApiError as e:
            await db.update_order_intent(self.conn, intent_id, status="REJECTED")
            await self.risk.record_order_rejection()
            await db.log_decision(
                self.conn, cycle_id, symbol, current_weight, target_weight, current_weight - target_weight,
                "N/A", "N/A", "SELL", qty, f"{exit_reason} 주문 거부: {e.msg1}", intent_id, context=context,
            )
            return

        await self.risk.add_daily_pnl((price_krw - avg_price_krw) * qty)
        await db.log_decision(
            self.conn, cycle_id, symbol, current_weight, target_weight, current_weight - target_weight,
            "N/A", "N/A", "SELL", qty, exit_reason, intent_id, context=context,
        )

    async def _process_overseas_symbol(
        self, cycle_id: int, symbol: str, exchange: str, target_weight: float,
        holding: Optional[dict], total_equity: float, ws_ok: bool,
    ) -> None:
        """해외주식 처리 (현재 미국 NASD/NYSE/AMEX만 실증). VI 개념이 없어 국내와 달리
        halted 반환이 없다 — 시장전체 이상 감지는 국내 종목 수만으로 판단한다."""
        assert self.conn is not None and self.risk is not None

        context: dict = {"target_weight": target_weight, "total_equity": total_equity, "exchange": exchange}

        if not ws_ok:
            await self._log_no_op(cycle_id, symbol, target_weight, "체결통보 웹소켓 비정상 - 신규주문 차단", context)
            return

        price_info = await kis_overseas.get_price(self.client, exchange, symbol)
        price_foreign = float(price_info.get("last") or 0)
        context["price_foreign"] = price_foreign
        if price_foreign <= 0:
            await self._log_no_op(cycle_id, symbol, target_weight, "현재가 조회 실패", context)
            return

        # 비중 계산은 KRW 기준으로 통일해야 하므로 기준환율(bass_exrt)이 필요하다 — 이미
        # 보유 중이면 이번 사이클 시작에 조회한 present_balance 값을 재사용하고, 신규
        # 종목(보유 없음)이면 매수가능금액조회에서 환율(exrt)만 얻어온다.
        bass_exrt = float(holding.get("bass_exrt")) if holding and holding.get("bass_exrt") else 0.0
        if bass_exrt <= 0:
            exrt_probe = await kis_overseas.get_buyable_cash(self.client, exchange, symbol, price_foreign)
            bass_exrt = float(exrt_probe.get("exrt") or 0)
            context["exrt_probe"] = exrt_probe
        if bass_exrt <= 0:
            await self._log_no_op(cycle_id, symbol, target_weight, "환율 조회 실패", context)
            return

        price_krw = price_foreign * bass_exrt
        qty = float(holding.get("cblc_qty13") or 0) if holding else 0.0
        position_value = qty * price_krw
        current_weight = (position_value / total_equity) if total_equity > 0 else 0.0
        context.update({
            "bass_exrt": bass_exrt, "price_krw": price_krw, "qty_before": qty,
            "position_value": position_value, "current_weight": current_weight,
        })

        # --- 손절/트레일링익절: 쿨다운/일일손실한도보다 먼저, 그것들과 무관하게 평가한다.
        # 해외는 시장가 미지원(FX 리스크로 의도적 배제)이라 직전가 대비 소폭 할인된 지정가로
        # 체결확률을 확보한다. 가격/평단가는 원화(KRW) 기준으로 일관되게 비교한다.
        if qty > 0:
            avg_price_foreign = float(holding.get("avg_unpr3") or 0) if holding else 0.0
            avg_price_krw = avg_price_foreign * bass_exrt
            await db.update_position_peak(self.conn, symbol, price_krw)
            position_row = await db.get_position(self.conn, symbol)
            peak_price = position_row["peak_price_since_entry"] if position_row else None
            exit_decision = exit_guard.evaluate_exit(avg_price_krw, peak_price, price_krw)
            context["exit_check"] = {
                "avg_price_krw": avg_price_krw, "peak_price_krw": peak_price,
                "fired": exit_decision.reason if exit_decision else None,
            }
            if exit_decision is not None:
                await self._submit_exit_order_overseas(
                    cycle_id, symbol, exchange, target_weight, current_weight, qty,
                    price_foreign, price_krw, avg_price_krw, exit_decision.reason, context,
                )
                return

        if await self.risk.is_cooldown_active(symbol):
            await self._log_no_op(cycle_id, symbol, target_weight, "쿨다운 중", context)
            return
        if await self.risk.is_daily_loss_limit_hit(total_equity):
            await self._log_no_op(cycle_id, symbol, target_weight, "일일 손실 한도 도달", context)
            return

        chart_rows = await kis_overseas.get_daily_chart(self.client, exchange, symbol)
        df = indicators.chart_rows_to_dataframe_overseas(chart_rows)
        tech_signal = indicators.compute_technical_signal(df)
        context["tech_signal"] = tech_signal

        allow_backfill = self._intraday_backfill_budget > 0
        self._intraday_backfill_budget -= 1
        intraday_signal = await intraday.get_intraday_signal_overseas(
            self.client, self.conn, symbol, exchange, allow_backfill
        )
        context["intraday_signal"] = intraday_signal

        sentiment_signal = None
        if self.llm_provider is not None:
            try:
                sentiment_signal = await news_sentiment.get_symbol_sentiment(self.conn, self.llm_provider, symbol)
            except Exception:
                logger.exception(f"'{symbol}' 뉴스 감성분석 파이프라인 오류 - 기술적 지표만 사용")
        context["sentiment_signal"] = sentiment_signal

        # 밸류에이션(PER/PBR/목표주가 컨센서스)은 국내 데이터 소스 기반이라 해외 종목은 미지원.
        context["valuation_signal"] = None

        action = await signal_engine.decide(
            current_weight=current_weight,
            target_weight=target_weight,
            band=settings.REBALANCE_BAND_PCT,
            tech_signal=tech_signal,
            intraday_signal=intraday_signal,
            sentiment_signal=sentiment_signal,
            valuation_signal=None,
            price=price_krw,  # signal_engine의 qty 계산은 KRW 기준 total_equity와 일관되어야 함
            current_position_qty=qty,
            current_position_value=position_value,
            total_equity=total_equity,
            risk=self.risk,
        )

        if action is None:
            await self._log_no_op(
                cycle_id, symbol, target_weight,
                f"조건 미충족 (tech={tech_signal}, intraday={intraday_signal}, sentiment={sentiment_signal})",
                context,
            )
            return

        context["action_before_recheck"] = {"side": action.side, "qty": action.qty, "reason": action.reason}

        # 주문 직전 항상 실시간으로 재확인 — 캐시/이전 조회 신뢰 금지.
        if action.side == "buy":
            psbl = await kis_overseas.get_buyable_cash(self.client, exchange, symbol, price_foreign)
            context["buyable_check"] = psbl
            action.qty = min(action.qty, float(psbl.get("max_ord_psbl_qty") or 0))
        else:
            # 해외는 매도가능수량 전용 실시간 재조회 API를 아직 연결하지 않았다 — 이번 사이클
            # 시작에 조회한 present_balance의 주문가능수량(ord_psbl_qty1)을 상한으로 쓴다.
            sellable = float(holding.get("ord_psbl_qty1") or 0) if holding else 0.0
            context["sellable_check"] = {"present_balance_ord_psbl_qty1": sellable}
            action.qty = min(action.qty, sellable)
        action.qty = float(int(action.qty))
        context["qty_after_recheck"] = action.qty

        if action.qty <= 0:
            await self._log_no_op(cycle_id, symbol, target_weight, "주문가능수량 0", context)
            return

        try:
            intent_id = await db.create_order_intent(
                self.conn, cycle_id, symbol, action.side, action.qty, "limit", price_foreign,
                action.reason, market="overseas",
            )
        except aiosqlite.IntegrityError:
            logger.warning(f"'{symbol}' 이번 사이클에 이미 intent 존재 — 주문 스킵")
            return

        try:
            result = await kis_overseas.order(
                self.client, exchange, symbol, action.side, int(action.qty), price_foreign,
            )
            kis_order_no = result.get("ODNO") or result.get("odno")
            await db.update_order_intent(self.conn, intent_id, status="SUBMITTED", kis_order_no=kis_order_no)
        except KisApiError as e:
            await db.update_order_intent(self.conn, intent_id, status="REJECTED")
            await self.risk.record_order_rejection()
            await db.log_decision(
                self.conn, cycle_id, symbol, current_weight, target_weight,
                current_weight - target_weight, tech_signal["direction"], str(sentiment_signal),
                action.side.upper(), action.qty, f"주문 거부: {e.msg1}", intent_id, context=context,
            )
            return

        await db.log_decision(
            self.conn, cycle_id, symbol, current_weight, target_weight,
            current_weight - target_weight, tech_signal["direction"], str(sentiment_signal),
            action.side.upper(), action.qty, action.reason, intent_id, context=context,
        )
