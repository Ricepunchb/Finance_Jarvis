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
from core.us_market.notifier import notify_news_surprise_if_needed, notify_signal_if_needed
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

        # 5. 알림 전송 (텔레그램: 60% 이상 강한 시그널 및 손절/익절 시에만 발송)
        action = sig_result.get("combined_action", "HOLD")
        exit_trigger = sig_result.get("exit_trigger", "NONE")
        current_alert_key = f"{action}:{exit_trigger}"
        last_alert_key = holding_info.get("last_alert_key") if holding_info else None

        mkt_status = get_us_market_status()
        alert_sent = False
        if send_notification and holding_info and holding_info.get("alert_enabled", 1):
            alert_sent = await notify_signal_if_needed(
                sig_result,
                market_status_str=mkt_status["status_label"],
                last_alert_key=last_alert_key,
            )

        # 알림이 실제로 발송되었거나, HOLD(관망) 상태로 복귀한 경우에만 DB의 last_alert_key 갱신
        # (60% 미만 약한 시그널일 때 미리 키를 덮어써서 향후 60% 이상 강한 시그널 알림이 누락되는 현상 방지)
        if holding_info:
            if alert_sent:
                await db.update_us_watchlist_alert_state(conn, clean_sym, current_alert_key)
            elif action == "HOLD" and exit_trigger == "NONE" and last_alert_key != "HOLD:NONE":
                await db.update_us_watchlist_alert_state(conn, clean_sym, "HOLD:NONE")

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


async def scan_us_market_news(
    conn: Optional[Any] = None,
    target_symbols: Optional[List[str]] = None,
    threshold: int = 75,
    send_notifications: bool = True,
    news_count_per_symbol: int = 8,
) -> Dict[str, Any]:
    """글로벌/미국장 주요 뉴스를 수집, 동일 사건 군집화(Clustering) 후 Surprise 점수를 산출하고
    임계값(threshold) 이상의 서프라이즈 뉴스에 대해 텔레그램 알림 발송 및 DB 기록을 수행한다.
    """
    close_conn = False
    if conn is None:
        conn = await db.get_connection()
        close_conn = True

    try:
        if not target_symbols:
            # 워치리스트 종목 조회
            wl_items = await db.list_us_watchlist(conn)
            symbols = [it["symbol"].upper() for it in wl_items if it.get("alert_enabled", 1)]
            # 기본 보유 포트폴리오 및 매크로 지수 보강
            core_symbols = ["NVDA", "PLTR", "SCHD", "SPCX", "TSLW", "VOO", "SGOV", "SPY", "QQQ"]
            for s in core_symbols:
                if s not in symbols:
                    symbols.append(s)
        else:
            symbols = [s.strip().upper() for s in target_symbols]

        # 1. 종목별 최신 뉴스 및 시세 병렬 수집
        from core.us_market.surprise import (
            NewsArticle,
            analyze_news_surprise,
            cluster_news_articles,
            fetch_latest_us_news,
        )

        llm = None
        try:
            from core.llm.factory import get_llm_provider
            llm = get_llm_provider()
        except Exception as e:
            logger.info(f"LLM Provider 초기화 불가 (휴리스틱 모드 작동): {e}")

        all_articles: List[NewsArticle] = []
        price_changes: Dict[str, float] = {}

        for sym in symbols:
            try:
                art_task = asyncio.to_thread(fetch_latest_us_news, sym, news_count_per_symbol)
                quote_task = get_us_quote(sym)
                arts, quote = await asyncio.gather(art_task, quote_task, return_exceptions=True)

                if isinstance(arts, list):
                    all_articles.extend(arts)
                if isinstance(quote, dict) and quote.get("change_pct") is not None:
                    price_changes[sym] = float(quote["change_pct"])
            except Exception as sym_err:
                logger.warning(f"'{sym}' 뉴스 수집 중 오류: {sym_err}")
            await asyncio.sleep(0.1)

        # 2. 동일 사건(Event) 군집화 (Jaccard 토큰 유사도 기반)
        clusters = cluster_news_articles(all_articles, similarity_threshold=0.38, time_window_hours=6.0)
        # 중요도 순으로 정렬 (다수 언론사 보도 클러스터 및 주가 급등락 종목 우선 분석)
        clusters.sort(
            key=lambda c: (c.count > 1, abs(price_changes.get(c.symbol, 0.0)) >= 2.0),
            reverse=True,
        )

        # 3. 클러스터별 Surprise 점수 산출 및 임계치 검사
        processed_clusters: List[Dict[str, Any]] = []
        alerted_clusters: List[Dict[str, Any]] = []

        for cl in clusters:
            # 24시간 이내 이미 발송된 클러스터 또는 3시간 이내 동일 종목 유사 사건인지 확인
            is_sent = await db.is_us_news_duplicate_or_recent(
                conn, cl.cluster_id, cl.symbol, cl.anchor.title, max_age_hours=24.0, cooldown_hours=3.0
            )
            if is_sent:
                logger.info(f"'{cl.symbol}' 클러스터({cl.cluster_id}) 또는 최근 유사 이슈 이미 발송됨 ➔ 알림 생략")
                continue

            current_change = price_changes.get(cl.symbol)
            analysis = await analyze_news_surprise(
                cluster=cl,
                llm_provider=llm,
                current_price_change_pct=current_change,
            )
            # LLM API 속도 제한(RPM) 보호용 미세 딜레이
            if llm is not None:
                await asyncio.sleep(0.3)

            cluster_summary_data = {
                "cluster_id": cl.cluster_id,
                "symbol": cl.symbol,
                "title": cl.anchor.title,
                "surprise_score": analysis.surprise_score,
                "impact_type": analysis.impact_type,
                "affected_sector": analysis.affected_sector_theme,
                "summary": "\n".join(analysis.summary_sentences),
                "summary_sentences": analysis.summary_sentences,
                "sources": cl.sources,
                "cluster_count": cl.count,
                "published_at": cl.anchor.published_at,
                "rationale": analysis.rationale,
            }
            processed_clusters.append(cluster_summary_data)

            # 임계값(Threshold) 이상일 때만 알림 발송 및 DB 기록
            if analysis.surprise_score >= threshold:
                alert_ok = False
                if send_notifications:
                    alert_ok, desc = await notify_news_surprise_if_needed(
                        symbol=cl.symbol,
                        analysis=analysis,
                        sources=cl.sources,
                        cluster_count=cl.count,
                        published_ts=cl.anchor.published_at,
                        threshold=threshold,
                    )
                else:
                    alert_ok = True

                # DB 기록
                await db.record_us_news_alert(
                    conn=conn,
                    cluster_id=cl.cluster_id,
                    symbol=cl.symbol,
                    title=cl.anchor.title,
                    surprise_score=analysis.surprise_score,
                    impact_type=analysis.impact_type,
                    affected_sector=analysis.affected_sector_theme,
                    summary="\n".join(analysis.summary_sentences),
                    source=", ".join(cl.sources[:3]),
                    cluster_count=cl.count,
                    published_at=cl.anchor.published_at,
                    sent_at=time.time(),
                    raw_data=cluster_summary_data,
                )
                alerted_clusters.append(cluster_summary_data)

        scores = [c["surprise_score"] for c in processed_clusters]
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
        min_score = min(scores) if scores else 0
        max_score = max(scores) if scores else 0
        filtered_count = len(processed_clusters) - len(alerted_clusters)

        return {
            "scanned_symbols": len(symbols),
            "total_articles": len(all_articles),
            "clusters_count": len(clusters),
            "processed_count": len(processed_clusters),
            "filtered_count": filtered_count,
            "alerted_count": len(alerted_clusters),
            "average_score": avg_score,
            "min_score": min_score,
            "max_score": max_score,
            "threshold": threshold,
            "processed_clusters": processed_clusters,
            "alerted_clusters": alerted_clusters,
        }
    finally:
        if close_conn:
            await conn.close()


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

                    # 글로벌 뉴스 인텔리전스 스캔 연계 (Surprise 75점 이상 텔레그램 발송)
                    logger.info("🇺🇸 글로벌 뉴스 Surprise 인텔리전스 스캔 시작")
                    news_res = await scan_us_market_news(threshold=75, send_notifications=True)
                    logger.info(f"🇺🇸 글로벌 뉴스 스캔 완료: {news_res.get('alerted_count', 0)}건 서프라이즈 알림 발송")

                    # 미국장 포트폴리오 리밸런싱 트리거 자동 진단 (해외 실전 계좌에서만 실행)
                    is_meritz = getattr(settings, "BROKER", "kis").lower() == "meritz"
                    is_domestic = getattr(settings, "DOMESTIC_ONLY", False)
                    if is_meritz and not is_domestic:
                        try:
                            from core.us_market.rebalance import (
                                analyze_us_portfolio_drift,
                                build_us_rebalance_plan,
                                format_us_rebalance_telegram_message,
                            )
                            from core.us_market.notifier import get_telegram_config, send_telegram_message_with_result
                            reb_conn = await db.get_connection()
                            try:
                                drift_res = await analyze_us_portfolio_drift(reb_conn)
                                plan = build_us_rebalance_plan(drift_res)
                                if plan.items and plan.trigger_type in ("CASH_INFLOW", "BAND_DRIFT"):
                                    last_alert_time_str = await db.get_state(reb_conn, "us_last_rebalance_alerted_at")
                                    last_alert_time = float(last_alert_time_str) if last_alert_time_str else 0.0
                                    if (time.time() - last_alert_time) > 43200:  # 12시간 쿨다운
                                        token, chat_id = get_telegram_config()
                                        if token and chat_id:
                                            msg = format_us_rebalance_telegram_message(plan)
                                            await asyncio.to_thread(
                                                send_telegram_message_with_result, token, chat_id, msg, parse_mode=""
                                            )
                                            await db.set_state(reb_conn, "us_last_rebalance_alerted_at", str(time.time()))
                                            logger.info(f"🇺🇸 미국장 리밸런싱 계획({plan.trigger_type}) 텔레그램 알림 전송 완료")
                            finally:
                                await reb_conn.close()
                        except Exception as reb_err:
                            logger.warning(f"미국장 리밸런싱 진단 중 오류: {reb_err}")
            except Exception:
                logger.exception("미국장 정기 스케줄러 루프 오류 발생")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass

