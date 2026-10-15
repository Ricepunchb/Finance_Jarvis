#!/usr/bin/env python3
"""해외 종목 리서치 리포트, 뉴스 수집 및 AI 인사이트 묶음 생성 스크립트."""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiosqlite
from core import db, discovery, discovery_signals
from core.discovery_sources import source_overseas_broker_research
from core.fundamentals.naver_research import fetch_company_research
from core.insights import InsightCollector, build_batch
from core.llm.factory import get_llm_provider
from core.news.naver_finance import fetch_recent_news
from core.news.sentiment import _signed_score
from core.utils import symbol_mapper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("collect_overseas_insights")


async def sync_insights_for_db(db_path: str) -> None:
    logger.info(f"==================================================")
    logger.info(f"🚀 인사이트 동기화 시작: {db_path}")
    logger.info(f"==================================================")

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        candidates = await db.list_candidate_universe(conn)
        ovrs_candidates = [c for c in candidates if symbol_mapper.is_overseas_symbol(c["symbol"])]
        ovrs_symbols = [c["symbol"] for c in ovrs_candidates]
        logger.info(f"등록된 해외 후보 종목 수: {len(ovrs_symbols)} ({', '.join(ovrs_symbols[:8])}...)")

        collector = InsightCollector()

        # 1. 국내 증권사 리포트 수집
        logger.info("국내 증권사 리포트 수집 중...")
        domestic_reports = await asyncio.to_thread(fetch_company_research, 2)
        collector.reports.extend(domestic_reports)
        logger.info(f"국내 리포트 수집 완료: {len(domestic_reports)}건")

        # 2. 해외 모닝스타 및 월가 리서치 수집
        logger.info("해외 모닝스타/월가 리서치 수집 중...")
        await source_overseas_broker_research(ovrs_symbols, collector)
        ovrs_report_count = len(collector.reports) - len(domestic_reports)
        logger.info(f"해외 리포트 수집 완료: {ovrs_report_count}건 (총합 {len(collector.reports)}건)")

        # 3. 해외 주요 종목 최신 뉴스 수집 및 LLM 감성 분석
        logger.info("해외 주요 종목 최신 뉴스 수집 및 감성 분석 중...")
        provider = get_llm_provider()
        target_news_symbols = [s for s in ovrs_symbols if s not in ("SPY", "QQQ", "GLD", "XLU")][:12]
        news_count = 0

        for sym in target_news_symbols:
            try:
                articles = await asyncio.to_thread(fetch_recent_news, sym, max_items=2)
                for a in articles:
                    cached = await db.get_cached_news_sentiment(conn, a["url"])
                    if cached is None:
                        try:
                            res = await provider.analyze_news(sym, a["title"], a.get("summary", ""))
                            score = _signed_score(res)
                            await db.cache_news_sentiment(
                                conn,
                                sym,
                                a["source"],
                                a["url"],
                                a.get("published_at"),
                                a["title"],
                                score,
                                res.get("reasoning", ""),
                            )
                            news_count += 1
                        except Exception as e:
                            logger.warning(f"뉴스 감성분석 실패 ({sym} - {a['title'][:20]}): {e}")
                    else:
                        news_count += 1
            except Exception as e:
                logger.warning(f"'{sym}' 뉴스 수집 예외: {e}")

        logger.info(f"해외 종목 뉴스 {news_count}건 캐시 완료")

        # 4. 점수 캐시 및 숏리스트 준비
        cache = await discovery.load_scored_cache(conn)
        scored_entries = cache.get("entries", []) if cache else []
        if not scored_entries:
            for c in candidates:
                scored_entries.append({
                    "symbol": c["symbol"],
                    "name": c["name"],
                    "score": 0.5,
                    "angle": "VALUE",
                    "angle_label": "기본후보",
                    "components": {},
                    "thesis": f"{c['name']} 관찰 후보",
                    "universe_tag": c.get("universe_tag", "SEED"),
                })

        summary = {
            "started_at": time.time(),
            "sources": {"broker": len(collector.reports), "news": news_count},
            "finished_at": time.time(),
        }

        # 5. 종합 인사이트 묶음 생성 및 Gemini AI 다이제스트 생성
        logger.info("AI 인사이트 종합 묶음(Batch) 빌드 및 Gemini 다이제스트 생성 중...")
        batch_res = await build_batch(
            conn, collector, scored_entries, summary, trigger_type="MANUAL"
        )
        logger.info(f"✅ 인사이트 묶음 생성 완료: Batch #{batch_res.get('batch_id')} (status={batch_res.get('status')})")
        logger.info(f"집계 결과: {batch_res.get('counts')}")

    finally:
        await conn.close()


async def main():
    db_paths = ["data/jarvis_mock.db", "data/jarvis_isa.db", "data/jarvis_real.db"]
    for path in db_paths:
        if Path(path).exists():
            await sync_insights_for_db(path)


if __name__ == "__main__":
    asyncio.run(main())

