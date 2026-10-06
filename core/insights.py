# core/insights.py
"""종목 발굴 및 뉴스/리포트 분석 결과를 묶음(batch) 단위로 영속화하고 AI 다이제스트를 생성한다.

발굴 갱신 1회 = 인사이트 묶음 1개.
장 마감 후 하루 1회 수집된 증권사 리포트, 시장 헤드라인 표본, 보유종목 뉴스 감성, 발굴 결과를
하나의 묶음으로 통합 저장하여 '인사이트' 탭에서 한눈에 조회할 수 있도록 한다.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db, discovery_signals
from core.config import settings
from core.fundamentals.naver_research import fetch_research_detail
from core.llm.factory import get_llm_provider
from core.news.sentiment import _dedupe_articles

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


@dataclass
class InsightCollector:
    """discovery_sources가 수집하는 원시 자료들을 묶음 빌더에 전달하는 컨테이너."""
    reports: List[Dict[str, Any]] = field(default_factory=list)
    headline_matches: List[Dict[str, Any]] = field(default_factory=list)
    sampled_headline_count: int = 0


async def _enrich_and_analyze_reports(
    conn: aiosqlite.Connection,
    reports: List[Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """리포트 메타데이터를 저장하고, 본문 미수집 건은 상세 API를 조회하며,
    미분석 건은 LLM 배치 분석을 수행한다."""
    if not reports:
        return {}

    # 1. 메타데이터 기본 upsert (해외 리포트 문자열 ID는 안전 정수로 변환)
    for r in reports:
        raw_id = r.get("research_id")
        if raw_id is not None and not isinstance(raw_id, int):
            try:
                r["research_id"] = int(raw_id)
            except (ValueError, TypeError):
                r["research_id"] = abs(hash(str(raw_id))) % 2_000_000_000

    await db.upsert_research_reports(conn, reports)

    r_ids = [int(r["research_id"]) for r in reports if r.get("research_id")]
    existing = await db.get_research_reports(conn, r_ids)


    # 2. 본문 텍스트가 없는 리포트 상세 수집 (최신순 최대 N건)
    need_detail = [
        rid for rid in r_ids
        if rid in existing and not (existing[rid].get("content_text") or "").strip()
    ][: settings.INSIGHT_REPORT_ANALYZE_MAX_PER_RUN]

    if need_detail:
        semaphore = asyncio.Semaphore(5)

        async def fetch_one(rid: int):
            async with semaphore:
                return await asyncio.to_thread(fetch_research_detail, rid)

        detail_results = await asyncio.gather(*(fetch_one(rid) for rid in need_detail))
        for d in detail_results:
            if d and d.get("content_text"):
                rid = d["research_id"]
                if rid in existing:
                    existing[rid].update(d)
                await db.update_research_report_detail(
                    conn,
                    research_id=rid,
                    content_text=d["content_text"],
                    opinion=d.get("opinion"),
                    target_price=d.get("target_price"),
                    price_at_write=d.get("price_at_write"),
                    attach_url=d.get("attach_url"),
                )

    # 3. LLM 분석이 아직 없는 리포트 선별
    need_analysis = [
        existing[rid] for rid in r_ids
        if rid in existing
        and not (existing[rid].get("analysis_json") or "").strip()
        and (existing[rid].get("content_text") or "").strip()
    ]

    if need_analysis:
        try:
            provider = get_llm_provider()
            batch_size = settings.INSIGHT_REPORT_LLM_BATCH_SIZE
            for i in range(0, len(need_analysis), batch_size):
                chunk = need_analysis[i: i + batch_size]
                analyses = await provider.analyze_research_reports(chunk)
                for rid, analysis in analyses.items():
                    await db.save_report_analysis(conn, rid, json.dumps(analysis, ensure_ascii=False))
                    if rid in existing:
                        existing[rid]["analysis_json"] = json.dumps(analysis, ensure_ascii=False)
        except Exception:
            logger.exception("인사이트 리포트 LLM 분석 중 예외 발생 - 수집된 본문만으로 계속 진행")

    return await db.get_research_reports(conn, r_ids)


async def build_batch(
    conn: aiosqlite.Connection,
    collector: InsightCollector,
    scored_entries: List[Dict[str, Any]],
    refresh_summary: Dict[str, Any],
    trigger_type: str = "SCHEDULED",
) -> Dict[str, Any]:
    """발굴 갱신 작업 직후 호출되어 1회의 종합 인사이트 묶음을 생성하고 영속화한다."""
    if not settings.INSIGHT_ENABLED:
        return {"status": "SKIPPED", "reason": "INSIGHT_ENABLED is False"}

    started_at = time.time()
    last_finished = await db.get_last_insight_batch_finished_at(conn)
    window_start = last_finished if last_finished is not None else (started_at - 86400)

    batch_id = await db.create_insight_batch(
        conn, trigger_type=trigger_type, started_at=started_at, window_start=window_start
    )
    logger.info(f"인사이트 묶음 #{batch_id} 생성 시작 (trigger={trigger_type})")

    items_to_add: List[Dict[str, Any]] = []
    status = "DONE"
    batch_error = None
    digest_error = None

    # (A) 증권사 리포트 처리
    try:
        enriched_reports = await _enrich_and_analyze_reports(conn, collector.reports)
        cur = await conn.execute(
            "SELECT DISTINCT ref_id FROM insight_items WHERE kind = 'broker_report' AND batch_id != ?",
            (batch_id,),
        )
        seen_ref_ids = {row["ref_id"] for row in await cur.fetchall() if row["ref_id"]}

        for r in collector.reports:
            rid = int(r["research_id"]) if r.get("research_id") else 0
            enriched = enriched_reports.get(rid, {})
            analysis_dict = json.loads(enriched.get("analysis_json") or "{}")

            payload = {
                "broker": r.get("broker"),
                "read_count": r.get("read_count", 0),
                "write_date": r.get("write_date") or r.get("date"),
                "opinion": enriched.get("opinion"),
                "target_price": enriched.get("target_price"),
                "price_at_write": enriched.get("price_at_write"),
                "attach_url": enriched.get("attach_url"),
                "end_url": enriched.get("end_url") or r.get("end_url"),
                "analysis": analysis_dict,
                "has_content": bool((enriched.get("content_text") or "").strip()),
            }

            is_new = str(rid) not in seen_ref_ids if rid else False
            items_to_add.append({
                "batch_id": batch_id,
                "kind": "broker_report",
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "title": r.get("title"),
                "source": r.get("broker"),
                "url": enriched.get("end_url") or r.get("end_url"),
                "published_at": None,
                "ref_id": str(rid) if rid else None,
                "is_new": is_new,
                "payload_json": json.dumps(payload, ensure_ascii=False),
            })
    except Exception as e:
        logger.exception("인사이트 리포트 항목 처리 실패")
        status = "PARTIAL"
        batch_error = f"리포트 처리 오류: {e}"

    # (B) 시장 뉴스 헤드라인 표본 처리 (중복 제거 및 상한 제한)
    try:
        seen_titles = set()
        deduped_headlines = []
        for h in collector.headline_matches:
            t = (h.get("title") or "").strip()
            if not t or t in seen_titles:
                continue
            seen_titles.add(t)
            deduped_headlines.append(h)

        for h in deduped_headlines[: settings.INSIGHT_HEADLINE_STORE_MAX]:
            payload = {
                "matched_symbols": h.get("matched_symbols", []),
                "mentions": h.get("mentions", 1),
                "sampled_headlines": collector.sampled_headline_count,
            }
            items_to_add.append({
                "batch_id": batch_id,
                "kind": "market_headline",
                "symbol": h.get("symbol"),
                "name": h.get("name"),
                "title": h.get("title"),
                "source": "kis_headline_sample",
                "url": None,
                "published_at": h.get("published_at"),
                "ref_id": None,
                "is_new": True,
                "payload_json": json.dumps(payload, ensure_ascii=False),
            })
    except Exception as e:
        logger.exception("인사이트 시장 헤드라인 항목 처리 실패")
        status = "PARTIAL"

    # (C) 보유종목 뉴스 감성 (window_start 이후 수집된 news_cache)
    try:
        cur = await conn.execute(
            "SELECT symbol, source, url, published_at, raw_text, sentiment_score, sentiment_reasoning, fetched_at "
            "FROM news_cache WHERE fetched_at >= ? ORDER BY fetched_at ASC",
            (window_start,),
        )
        news_rows = [dict(r) for r in await cur.fetchall()]
        articles_to_dedupe = [
            {
                "symbol": r["symbol"],
                "source": r["source"],
                "url": r["url"],
                "title": r["raw_text"],
                "published_at": r["published_at"],
                "score": r["sentiment_score"],
                "reasoning": r["sentiment_reasoning"],
            }
            for r in news_rows
        ]
        cleaned_articles = _dedupe_articles(articles_to_dedupe)

        for a in cleaned_articles:
            payload = {
                "score": a.get("score"),
                "reasoning": a.get("reasoning"),
            }
            items_to_add.append({
                "batch_id": batch_id,
                "kind": "holding_news",
                "symbol": a.get("symbol"),
                "name": None,
                "title": a.get("title"),
                "source": a.get("source"),
                "url": a.get("url"),
                "published_at": a.get("published_at"),
                "ref_id": None,
                "is_new": True,
                "payload_json": json.dumps(payload, ensure_ascii=False),
            })
    except Exception as e:
        logger.exception("인사이트 보유종목 뉴스 항목 처리 실패")
        status = "PARTIAL"

    # (D) 발굴 결과 후보 종목
    try:
        held_symbols = {row["symbol"] for row in await db.list_portfolio_symbols(conn)}
        shortlist = discovery_signals.select_with_quota(
            scored_entries, settings.DISCOVERY_TOP_N, settings.DISCOVERY_ANGLE_QUOTA,
            exclude=held_symbols, min_score=settings.DISCOVERY_MIN_SCORE,
        )
        shortlisted_symbols = {c["symbol"] for c in shortlist}

        for c in scored_entries:
            payload = {
                "score": c.get("score"),
                "angle": c.get("angle"),
                "angle_label": c.get("angle_label"),
                "components": c.get("components"),
                "thesis": c.get("thesis"),
                "sources": list((c.get("sources") or {}).keys()),
                "per": c.get("per"),
                "pbr": c.get("pbr"),
                "target_gap_pct": c.get("target_gap_pct"),
                "news_accel": c.get("news_accel"),
                "vol_ratio_5_20": c.get("vol_ratio_5_20"),
                "ret_5d_pct": c.get("ret_5d_pct"),
                "shortlisted": c["symbol"] in shortlisted_symbols,
            }
            items_to_add.append({
                "batch_id": batch_id,
                "kind": "candidate",
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "title": c.get("angle_label"),
                "source": c.get("universe_tag"),
                "url": None,
                "published_at": None,
                "ref_id": None,
                "is_new": c["symbol"] in shortlisted_symbols,
                "payload_json": json.dumps(payload, ensure_ascii=False),
            })
    except Exception as e:
        logger.exception("인사이트 발굴 후보 항목 처리 실패")
        status = "PARTIAL"

    # 아이템 일괄 저장
    await db.add_insight_items(conn, items_to_add)

    # 카운트 집계
    counts = {
        "broker_report": sum(1 for i in items_to_add if i["kind"] == "broker_report"),
        "new_report": sum(1 for i in items_to_add if i["kind"] == "broker_report" and i["is_new"]),
        "market_headline": sum(1 for i in items_to_add if i["kind"] == "market_headline"),
        "holding_news": sum(1 for i in items_to_add if i["kind"] == "holding_news"),
        "candidate": sum(1 for i in items_to_add if i["kind"] == "candidate"),
        "shortlist": sum(
            1 for i in items_to_add
            if i["kind"] == "candidate" and json.loads(i.get("payload_json") or "{}").get("shortlisted")
        ),
    }

    # (E) AI 다이제스트 생성 (Gemini 1회 호출)
    digest_data = None
    if settings.INSIGHT_DIGEST_ENABLED:
        try:
            active_weights = await db.get_active_target_weights(conn)
            holdings_list = [{"symbol": s, "weight": active_weights.get(s, 0.0)} for s in active_weights]

            top_reports = [
                {
                    "symbol": i["symbol"], "name": i["name"], "broker": i["source"], "title": i["title"],
                    "opinion": json.loads(i["payload_json"]).get("opinion"),
                    "target_price": json.loads(i["payload_json"]).get("target_price"),
                    "stance": json.loads(i["payload_json"]).get("analysis", {}).get("stance"),
                }
                for i in items_to_add if i["kind"] == "broker_report" and i["is_new"]
            ][:15]

            digest_context = {
                "holdings": holdings_list,
                "new_reports": top_reports,
                "market_headlines": [
                    {"symbol": i["symbol"], "name": i["name"], "title": i["title"],
                     "mentions": json.loads(i["payload_json"]).get("mentions", 1)}
                    for i in items_to_add if i["kind"] == "market_headline"
                ][:12],
                "holding_news": [
                    {"symbol": i["symbol"], "title": i["title"],
                     "score": json.loads(i["payload_json"]).get("score", 0),
                     "reasoning": json.loads(i["payload_json"]).get("reasoning", "")}
                    for i in items_to_add if i["kind"] == "holding_news"
                ][:10],
                "candidates": [
                    {"symbol": c["symbol"], "name": c.get("name"),
                     "score": c.get("score"), "angle_label": c.get("angle_label"), "thesis": c.get("thesis")}
                    for c in shortlist[:10]
                ],
            }

            provider = get_llm_provider()
            digest_data = await provider.summarize_insight_batch(digest_context)
        except Exception as e:
            logger.exception("AI 다이제스트 생성 실패")
            digest_error = str(e)
            if status == "DONE":
                status = "PARTIAL"

    # 1년 지난 과거 묶음 정리
    try:
        cleaned_count = await db.cleanup_old_insights(conn, settings.INSIGHT_RETENTION_DAYS)
        if cleaned_count > 0:
            logger.info(f"보관 기간({settings.INSIGHT_RETENTION_DAYS}일) 초과 인사이트 묶음 {cleaned_count}건 정리 완료")
    except Exception:
        logger.exception("과거 인사이트 묶음 정리 실패")

    finished_at = time.time()
    await db.finish_insight_batch(
        conn,
        batch_id=batch_id,
        status=status,
        finished_at=finished_at,
        counts_json=json.dumps(counts, ensure_ascii=False),
        refresh_summary_json=json.dumps(refresh_summary, ensure_ascii=False),
        digest_json=json.dumps(digest_data, ensure_ascii=False) if digest_data else None,
        digest_error=digest_error,
        error=batch_error,
    )

    logger.info(f"인사이트 묶음 #{batch_id} 완료 (status={status}, items={len(items_to_add)})")
    return {
        "batch_id": batch_id,
        "status": status,
        "counts": counts,
        "finished_at": finished_at,
        "has_digest": digest_data is not None,
    }


async def generate_batch_digest(
    conn: aiosqlite.Connection, batch_id: int, provider: Optional[Any] = None
) -> Dict[str, Any]:
    """기존 묶음의 데이터를 읽어 AI 다이제스트를 (재)생성한다."""
    batch = await db.get_insight_batch(conn, batch_id)
    if not batch:
        raise ValueError(f"묶음 #{batch_id}를 찾을 수 없습니다.")

    items = await db.list_insight_items(conn, batch_id)
    active_weights = await db.get_active_target_weights(conn)
    holdings_list = [{"symbol": s, "weight": active_weights.get(s, 0.0)} for s in active_weights]

    top_reports = [
        {
            "symbol": i["symbol"], "name": i["name"], "broker": i["source"], "title": i["title"],
            "opinion": json.loads(i.get("payload_json") or "{}").get("opinion"),
            "target_price": json.loads(i.get("payload_json") or "{}").get("target_price"),
            "stance": json.loads(i.get("payload_json") or "{}").get("analysis", {}).get("stance"),
        }
        for i in items if i["kind"] == "broker_report" and i["is_new"]
    ][:15]

    candidates = [
        {
            "symbol": i["symbol"], "name": i["name"],
            "score": json.loads(i.get("payload_json") or "{}").get("score"),
            "angle_label": json.loads(i.get("payload_json") or "{}").get("angle_label"),
            "thesis": json.loads(i.get("payload_json") or "{}").get("thesis"),
        }
        for i in items if i["kind"] == "candidate" and json.loads(i.get("payload_json") or "{}").get("shortlisted")
    ][:10]

    digest_context = {
        "holdings": holdings_list,
        "new_reports": top_reports,
        "market_headlines": [
            {"symbol": i["symbol"], "name": i["name"], "title": i["title"],
             "mentions": json.loads(i.get("payload_json") or "{}").get("mentions", 1)}
            for i in items if i["kind"] == "market_headline"
        ][:12],
        "holding_news": [
            {"symbol": i["symbol"], "title": i["title"],
             "score": json.loads(i.get("payload_json") or "{}").get("score", 0),
             "reasoning": json.loads(i.get("payload_json") or "{}").get("reasoning", "")}
            for i in items if i["kind"] == "holding_news"
        ][:10],
        "candidates": candidates,
    }

    if provider is None:
        provider = get_llm_provider()
    digest_data = await provider.summarize_insight_batch(digest_context)

    await db.finish_insight_batch(
        conn,
        batch_id=batch_id,
        status="DONE",
        digest_json=json.dumps(digest_data, ensure_ascii=False),
        digest_error=None,
    )
    return digest_data
