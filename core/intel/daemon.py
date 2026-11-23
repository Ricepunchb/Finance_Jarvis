# core/intel/daemon.py
"""독립 마스터 인텔리전스 데몬 (`intel-master`).

역할:
1. 모든 계좌 프로필 DB(`data/jarvis_*.db`)에서 모니터링/보유 종목 합집합을 수집한다.
2. 미국장 글로벌 뉴스(30분 주기) 및 국내 종목 뉴스(30분 주기)를 단일 프로세스에서
   수집 → EventDeduper(2단계 중복 제거·노드 병합) → Tier 1/2 분석 → `data/jarvis_intel.db` 저장.
3. 75점 미만 미장 뉴스도 사건 노드로 영속화하여 30분 주기 무한 재분석을 원천 차단한다.
4. `InstanceLock(settings.INTEL_LOCK_PATH)`로 단일 마스터 인스턴스만 실행되도록 보장한다.

실행:
  INTEL_ROLE=master uv run python -m core.intel.daemon
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.config import settings
from core.intel import db as intel_db
from core.intel.event_deduper import (
    EventDeduper,
    build_cluster_alias_key,
    build_url_alias_key,
)
from core.intel.usage_tracker import UsageTracker
from core.lock import InstanceLock

logger = logging.getLogger(__name__)

US_CORE_SYMBOLS: Tuple[str, ...] = (
    "NVDA", "PLTR", "SCHD", "SPCX", "TSLW", "VOO", "SGOV", "SPY", "QQQ",
)


def discover_union_symbols(data_dir: Optional[Path] = None) -> Tuple[List[str], List[str]]:
    """모든 계좌 프로필 DB(`data/jarvis_*.db`)에서 US/KR 활성 종목 합집합을 수집한다."""
    root_dir = data_dir or Path(settings.DB_PATH).parent
    us_symbols: Set[str] = set(US_CORE_SYMBOLS)
    kr_symbols: Set[str] = set()

    if not root_dir.exists():
        return sorted(us_symbols), sorted(kr_symbols)

    for db_file in sorted(root_dir.glob("jarvis_*.db")):
        if db_file.name == Path(settings.INTEL_DB_PATH).name:
            continue
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2.0)
            cur = conn.cursor()
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            if "us_watchlist" in tables:
                for (sym,) in cur.execute("SELECT symbol FROM us_watchlist WHERE alert_enabled = 1").fetchall():
                    if sym:
                        us_symbols.add(str(sym).strip().upper())

            if "target_weights" in tables:
                for sym, market in cur.execute(
                    "SELECT symbol, market FROM target_weights WHERE active = 1 AND target_weight > 0"
                ).fetchall():
                    if not sym:
                        continue
                    clean = str(sym).strip().upper()
                    if str(market or "domestic").lower() == "overseas":
                        us_symbols.add(clean)
                    else:
                        kr_symbols.add(clean)
            conn.close()
        except Exception as e:
            logger.debug(f"프로필 DB '{db_file.name}' 종목 수집 생략: {e}")

    return sorted(us_symbols), sorted(kr_symbols)


class IntelMasterDaemon:
    """중앙 마스터 인텔리전스 수집·중복제거·분석 데몬."""

    def __init__(
        self,
        *,
        us_interval_sec: int = 1800,
        kr_interval_sec: int = 1800,
        intel_db_path: Optional[str] = None,
        lock_path: Optional[str] = None,
        deduper: Optional[EventDeduper] = None,
        usage_tracker: Optional[UsageTracker] = None,
    ) -> None:
        self.us_interval_sec = us_interval_sec
        self.kr_interval_sec = kr_interval_sec
        self.intel_db_path = intel_db_path or settings.INTEL_DB_PATH
        self.lock = InstanceLock(lock_path or settings.INTEL_LOCK_PATH)
        self.deduper = deduper or EventDeduper()
        self.tracker = usage_tracker or UsageTracker(db_path=self.intel_db_path)
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run_us_news_cycle(
        self,
        *,
        symbols: Optional[List[str]] = None,
        threshold: int = 75,
        send_notifications: bool = True,
    ) -> Dict[str, Any]:
        """미국장 뉴스 수집 → 2단계 EventDeduper → 신규 사건만 분석 → 공유 DB 영속화."""
        from core.us_market.client import get_us_quote
        from core.us_market.notifier import notify_news_surprise_if_needed
        from core.us_market.surprise import (
            NewsArticle,
            analyze_news_surprise,
            cluster_news_articles,
            fetch_latest_us_news,
        )

        if symbols is None:
            symbols, _ = discover_union_symbols()

        llm = None
        try:
            from core.llm.factory import get_llm_provider
            llm = get_llm_provider()
        except Exception as e:
            logger.info(f"[intel-master] LLM 미연결(휴리스틱 모드): {e}")

        all_articles: List[NewsArticle] = []
        price_changes: Dict[str, float] = {}
        for sym in symbols:
            try:
                arts, quote = await asyncio.gather(
                    asyncio.to_thread(fetch_latest_us_news, sym, 8),
                    get_us_quote(sym),
                    return_exceptions=True,
                )
                if isinstance(arts, list):
                    all_articles.extend(arts)
                if isinstance(quote, dict) and quote.get("change_pct") is not None:
                    price_changes[sym] = float(quote["change_pct"])
            except Exception as e:
                logger.warning(f"[intel-master] '{sym}' US 뉴스 수집 오류: {e}")
            await asyncio.sleep(0.1)

        clusters = cluster_news_articles(all_articles, similarity_threshold=0.38, time_window_hours=6.0)
        clusters.sort(
            key=lambda c: (c.count > 1, abs(price_changes.get(c.symbol, 0.0)) >= 2.0),
            reverse=True,
        )

        skipped_exact = 0
        skipped_similar = 0
        analyzed_new = 0
        alerted_count = 0

        await intel_db.init_intel_db(self.intel_db_path)
        conn = await intel_db.get_intel_connection(self.intel_db_path)
        try:
            for cl in clusters:
                alias_key = build_cluster_alias_key(cl.cluster_id)
                match = await self.deduper.resolve_or_bind(
                    conn,
                    alias_key=alias_key,
                    symbol=cl.symbol,
                    market="US",
                    title=cl.anchor.title,
                    source=", ".join(cl.sources[:3]),
                    published_at=cl.anchor.published_at,
                )
                if match.matched:
                    if match.match_type == "exact_alias":
                        skipped_exact += 1
                    else:
                        skipped_similar += 1
                    continue

                analysis = await analyze_news_surprise(
                    cluster=cl,
                    llm_provider=llm,
                    current_price_change_pct=price_changes.get(cl.symbol),
                    summary_threshold=threshold,
                )
                analyzed_new += 1

                is_alert = False
                if analysis.surprise_score >= threshold and send_notifications:
                    ok, _ = await notify_news_surprise_if_needed(
                        symbol=cl.symbol,
                        analysis=analysis,
                        sources=cl.sources,
                        cluster_count=cl.count,
                        published_ts=cl.anchor.published_at,
                        threshold=threshold,
                    )
                    is_alert = bool(ok)
                    if is_alert:
                        alerted_count += 1

                # 75점 미만 포함 전량 노드로 저장하여 30분 주기 재분석을 원천 차단
                extra_aliases = [
                    build_url_alias_key(a.url) for a in cl.articles if getattr(a, "url", "")
                ]
                sentiment_dir = (
                    "BUY" if analysis.impact_type == "호재"
                    else "SELL" if analysis.impact_type == "악재"
                    else "HOLD"
                )
                sentiment_val = (
                    (analysis.surprise_score / 100.0) if sentiment_dir == "BUY"
                    else -(analysis.surprise_score / 100.0) if sentiment_dir == "SELL"
                    else 0.0
                )
                await self.deduper.register_node(
                    conn,
                    alias_key=alias_key,
                    symbol=cl.symbol,
                    market="US",
                    title=cl.anchor.title,
                    source=", ".join(cl.sources[:3]),
                    published_at=cl.anchor.published_at,
                    surprise_score=analysis.surprise_score,
                    sentiment_direction=sentiment_dir,
                    sentiment_score=sentiment_val,
                    sentiment_reasoning=analysis.rationale,
                    impact_type=analysis.impact_type,
                    affected_sector=analysis.affected_sector_theme,
                    summary_ko="\n".join(analysis.summary_sentences),
                    alerted=is_alert,
                    model_used=getattr(settings, "GEMINI_FAST_MODEL", "heuristic"),
                    extra_alias_keys=extra_aliases,
                )

            await intel_db.set_daemon_state(conn, "last_us_scan_at", str(time.time()))
        finally:
            await conn.close()

        summary = {
            "symbols": len(symbols),
            "clusters": len(clusters),
            "skipped_exact": skipped_exact,
            "skipped_similar": skipped_similar,
            "analyzed_new": analyzed_new,
            "alerted": alerted_count,
        }
        logger.info(f"[intel-master] US 뉴스 사이클 완료: {summary}")
        return summary

    async def run_kr_news_cycle(
        self,
        *,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """국내 보유/관심 종목 뉴스 수집 → EventDeduper → 신규 기사만 분석 → 공유 DB 저장."""
        from core.news.naver_finance import fetch_recent_news
        from core.news.sentiment import MAX_NEWS_LOOKBACK_HOURS, _dedupe_articles, _signed_score

        if symbols is None:
            _, symbols = discover_union_symbols()
        if not symbols:
            return {"symbols": 0, "skipped": 0, "analyzed_new": 0}

        llm = None
        try:
            from core.llm.factory import get_llm_provider
            llm = get_llm_provider()
        except Exception as e:
            logger.info(f"[intel-master] KR 뉴스 LLM 미연결: {e}")
            return {"symbols": len(symbols), "skipped": 0, "analyzed_new": 0}

        skipped = 0
        analyzed_new = 0
        await intel_db.init_intel_db(self.intel_db_path)
        conn = await intel_db.get_intel_connection(self.intel_db_path)
        try:
            for sym in symbols:
                try:
                    articles = await asyncio.to_thread(
                        fetch_recent_news, sym, settings.NEWS_MAX_ARTICLES_PER_SYMBOL
                    )
                    if not articles:
                        continue
                    for art in _dedupe_articles(articles):
                        pub_at = art.get("published_at")
                        age_hours = (time.time() - pub_at) / 3600.0 if pub_at is not None else 0.0
                        alias_key = build_url_alias_key(art["url"])

                        match = await self.deduper.resolve_or_bind(
                            conn,
                            alias_key=alias_key,
                            symbol=sym,
                            market="KR",
                            title=art["title"],
                            source=art.get("source"),
                            published_at=pub_at,
                        )
                        if match.matched:
                            skipped += 1
                            continue

                        if pub_at is not None and age_hours > MAX_NEWS_LOOKBACK_HOURS:
                            direction, score, reasoning = "HOLD", 0.0, f"72시간 초과 중립 캐싱 ({int(age_hours)}h)"
                        else:
                            res = await llm.analyze_news(sym, art["title"], art.get("summary", ""))
                            direction = res.get("direction", "HOLD")
                            score = _signed_score(res)
                            reasoning = res.get("reasoning", "")
                            analyzed_new += 1

                        await self.deduper.register_node(
                            conn,
                            alias_key=alias_key,
                            symbol=sym,
                            market="KR",
                            title=art["title"],
                            source=art.get("source"),
                            published_at=pub_at,
                            sentiment_direction=direction,
                            sentiment_score=score,
                            sentiment_reasoning=reasoning,
                            model_used=getattr(settings, "GEMINI_FAST_MODEL", "gemini"),
                        )
                except Exception as e:
                    logger.warning(f"[intel-master] KR '{sym}' 뉴스 처리 오류: {e}")

            await intel_db.set_daemon_state(conn, "last_kr_scan_at", str(time.time()))
        finally:
            await conn.close()

        summary = {"symbols": len(symbols), "skipped": skipped, "analyzed_new": analyzed_new}
        logger.info(f"[intel-master] KR 뉴스 사이클 완료: {summary}")
        return summary

    async def run_forever(self) -> None:
        """단일 락을 획득하고 US/KR 인텔리전스 수집 루프를 상시 실행한다."""
        self.lock.acquire()
        await intel_db.init_intel_db(self.intel_db_path)

        conn = await intel_db.get_intel_connection(self.intel_db_path)
        try:
            await intel_db.set_daemon_state(conn, "pid", str(os.getpid()))
            await intel_db.set_daemon_state(conn, "started_at", str(time.time()))
        finally:
            await conn.close()

        logger.info(f"🧠 intel-master 데몬 기동 완료 (PID={os.getpid()}, DB={self.intel_db_path})")
        try:
            while not self._stop_event.is_set():
                try:
                    us_syms, kr_syms = discover_union_symbols()
                    await self.run_us_news_cycle(symbols=us_syms, send_notifications=True)
                    await self.run_kr_news_cycle(symbols=kr_syms)
                    conn = await intel_db.get_intel_connection(self.intel_db_path)
                    try:
                        await intel_db.set_daemon_state(conn, "heartbeat_at", str(time.time()))
                    finally:
                        await conn.close()
                except Exception:
                    logger.exception("[intel-master] 주기 실행 중 예외 — 다음 사이클로 계속")

                sleep_sec = min(self.us_interval_sec, self.kr_interval_sec)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.lock.release()
            logger.info("🧠 intel-master 데몬 정상 종료")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    daemon = IntelMasterDaemon()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(daemon.run_forever())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
