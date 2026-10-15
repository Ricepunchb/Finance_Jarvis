# core/us_market/service.py
"""미국장 30분 모니터링 및 시그널 스캔 서비스 & 백그라운드 스케줄러.

FastAPI 서버 또는 UI에서 호출하여 모니터링 종목들을 일괄 스캔하고, 지표 분석,
DB 영속화 및 텔레그램 알림을 오케스트레이션합니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import time
from typing import Any, Dict, List, Optional

from core import db
from core.us_market.client import (
    get_us_30m_bars,
    get_us_consensus_and_news,
    get_us_daily_bars,
    get_us_market_status,
    get_us_quote,
)
from core.us_market.notifier import notify_signal_if_needed
from core.us_market.signals import compute_us_signals

logger = logging.getLogger(__name__)

# 기본 스캔 주기 (30분 = 1800초)
US_CYCLE_INTERVAL_SEC = 1800


async def scan_single_symbol(
    conn,
    symbol: str,
    item_info: Optional[Dict[str, Any]] = None,
    send_notification: bool = True,
) -> Dict[str, Any]:
    """단일 종목의 30분봉/일봉/호가를 수집하고 시그널을 산출 및 저장한다."""
    clean_sym = symbol.strip().upper()
    try:
        # 1. 시세, 30분봉, 일봉, 컨센서스 동시 비동기 수집
        quote_task = get_us_quote(clean_sym)
        bars_30m_task = get_us_30m_bars(clean_sym, period="1mo")
        bars_daily_task = get_us_daily_bars(clean_sym, period="3mo")
        consensus_task = get_us_consensus_and_news(clean_sym)

        quote, df_30m, df_daily, consensus = await asyncio.gather(
            quote_task, bars_30m_task, bars_daily_task, consensus_task, return_exceptions=False
        )

        current_price = quote.get("price") or (float(df_30m["close"].iloc[-1]) if not df_30m.empty else 0.0)
        if current_price <= 0:
            logger.warning(f"'{clean_sym}' 현재가 수집 불가 — 스킵")
            return {"symbol": clean_sym, "error": "현재가 수집 불가"}

        # 종목명 업데이트 (DB에 없거나 기본값인 경우 보강)
        if quote.get("name") and quote["name"] != clean_sym:
            await db.upsert_us_watchlist_item(
                conn,
                clean_sym,
                name=quote.get("name"),
                sector=quote.get("sector"),
            )

        # 2. 포지션 최고가(peak_price) 갱신
        await db.update_us_watchlist_peak(conn, clean_sym, current_price)
        latest_item = await db.get_us_watchlist_item(conn, clean_sym)
        holding_info = latest_item or item_info

        # 3. 시그널 및 손절/익절 종합 계산
        sig_result = compute_us_signals(
            symbol=clean_sym,
            df_30m=df_30m,
            df_daily=df_daily,
            current_price=current_price,
            holding=holding_info,
            consensus=consensus,
        )

        # 4. DB에 30분 시그널 기록 영구 보존
        change_pct = quote.get("change_pct")
        snap = {
            "daily": sig_result.get("daily_snapshot"),
            "intraday": sig_result.get("intraday_snapshot"),
            "quote": quote,
            "consensus": consensus,
        }
        await db.record_us_signal(
            conn=conn,
            symbol=clean_sym,
            price=current_price,
            change_pct=change_pct,
            daily_signal=json.dumps(sig_result.get("daily_signal")),
            intraday_signal=json.dumps(sig_result.get("intraday_signal")),
            combined_action=sig_result.get("combined_action", "HOLD"),
            strength=sig_result.get("strength", 0.0),
            exit_trigger=sig_result.get("exit_trigger", "NONE"),
            reason=sig_result.get("action_recommendation", ""),
            indicators_json=json.dumps(snap),
        )

        # 5. 알림 전송 (텔레그램: 시그널 변경 시에만 발송)
        action = sig_result.get("combined_action", "HOLD")
        exit_trigger = sig_result.get("exit_trigger", "NONE")
        current_alert_key = f"{action}:{exit_trigger}"
        last_alert_key = holding_info.get("last_alert_key") if holding_info else None

        mkt_status = get_us_market_status()
        if send_notification and holding_info and holding_info.get("alert_enabled", 1):
            await notify_signal_if_needed(
                sig_result,
                market_status_str=mkt_status["status_label"],
                last_alert_key=last_alert_key,
            )

        # 시그널 상태 변경 시 DB의 last_alert_key 갱신 (HOLD 전환도 상태 변경으로 기록하여 다음 BUY 전환 감지)
        if holding_info and current_alert_key != last_alert_key:
            await db.update_us_watchlist_alert_state(conn, clean_sym, current_alert_key)

        # 응답 패키징
        return {
            "symbol": clean_sym,
            "name": quote.get("name") or (holding_info.get("name") if holding_info else clean_sym),
            "sector": quote.get("sector") or (holding_info.get("sector") if holding_info else "US Equity"),
            "price": current_price,
            "change": quote.get("change"),
            "change_pct": change_pct,
            "prev_close": quote.get("prev_close"),
            "w52_high": quote.get("w52_high"),
            "w52_low": quote.get("w52_low"),
            "signals": sig_result,
            "holding": holding_info,
            "consensus": consensus,
            "scanned_at": time.time(),
        }
    except Exception as e:
        logger.exception(f"'{clean_sym}' 미국장 스캔 중 오류: {e}")
        return {"symbol": clean_sym, "error": str(e)}


async def scan_all_us_watchlist(
    send_notifications: bool = True,
    target_symbols: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """DB에 등록된 모든 미국장 모니터링 종목(또는 지정된 종목)을 스캔한다."""
    conn = await db.get_connection()
    try:
        items = await db.list_us_watchlist(conn)
    finally:
        await conn.close()

    if not items:
        return []

    item_map = {it["symbol"]: it for it in items}
    symbols_to_scan = target_symbols if target_symbols else list(item_map.keys())

    results = []
    for sym in symbols_to_scan:
        conn = await db.get_connection()
        try:
            res = await scan_single_symbol(
                conn=conn,
                symbol=sym,
                item_info=item_map.get(sym),
                send_notification=send_notifications,
            )
            results.append(res)
        finally:
            await conn.close()
        # 요청 간 미세 딜레이 (yfinance rate-limit 방지)
        await asyncio.sleep(0.2)

    return results


class USMarketMonitorScheduler:
    """미국장 30분 주기 백그라운드 모니터링 스케줄러."""

    def __init__(self, interval_sec: int = US_CYCLE_INTERVAL_SEC):
        self.interval_sec = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.is_running = False
        self.last_scan_at: Optional[float] = None
        self.last_scan_count: int = 0

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("🇺🇸 USMarketMonitorScheduler 시작됨 (30분 주기)")

    async def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        self._stop_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        logger.info("🇺🇸 USMarketMonitorScheduler 정지됨")

    async def _run_loop(self):
        # 서버 기동 직후 lifespan 초기화 완료를 위해 5초 대기 후 첫 스캔 시작
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
            return  # 대기 중 종료 신호 수신 시 바로 반환
        except asyncio.TimeoutError:
            pass

        while not self._stop_event.is_set():
            try:
                status = get_us_market_status()
                # 정규장이거나 주말이 아닌 경우 활성 스캔 (필요 시 상시 스캔)
                if not status["is_weekend"]:
                    logger.info(f"🇺🇸 미국장 정기 30분 스캔 시작 ({status['status_label']})")
                    results = await scan_all_us_watchlist(send_notifications=True)
                    self.last_scan_at = time.time()
                    self.last_scan_count = len(results)
                    logger.info(f"🇺🇸 미국장 정기 스캔 완료: {len(results)}개 종목 처리")
            except Exception:
                logger.exception("미국장 정기 스케줄러 루프 오류 발생")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass

