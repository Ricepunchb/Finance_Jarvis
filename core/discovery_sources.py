# core/discovery_sources.py
"""동적 종목 발굴 소스 + 하루 1회 갱신 작업.

모집단은 KIS 전종목 마스터(core/master_files.py)이고, 각 소스는 그중 "오늘 볼 만한 이유가
있는" 종목만 골라 {symbol: evidence}로 돌려준다. 소스 하나가 실패해도(비공식 API 변경,
모의투자 미지원 등) 경고만 남기고 나머지 소스로 계속 진행한다.

  momentum  KIS 등락률 상승 순위 + 거래량 증가율 순위 (당일)
  broker    네이버 증권사 '종목분석' 리포트 목록 (최근 N일)
  news      KIS 시장 전체 뉴스 헤드라인 표본에서 종목 언급 횟수
  value     시총 상위 풀의 네이버 컨센서스 목표가 괴리율
  theme     KIS 테마 마스터 기준 테마 동료가 급등했는데 아직 안 오른 종목
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db, discovery, discovery_signals, kis_domestic, master_files, quant_metrics
from core.config import settings
from core.fundamentals.naver_consensus import fetch_consensus
from core.fundamentals.naver_research import fetch_company_research
from core.kis_client import AsyncKISClient
from core.kis_common import KisApiError

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

MASTER_MAX_AGE_SEC = 20 * 3600
MOMENTUM_MIN_RISE_PCT = 3.0
NEWS_SAMPLE_HOURS = ["100000", "120000", "140000", "160000", "180000"]
NEWS_SAMPLE_DAYS = 3
NEWS_MIN_MENTIONS = 3
NEWS_MAX_SYMBOLS = 15
NEWS_MIN_NAME_LEN = 3
VALUE_MAX_SYMBOLS = 20
VALUE_FETCH_CONCURRENCY = 5
# 여러 소스에 동시에 잡히면 우선. 동률이면 이 순서로 자른다 (근거가 구체적인 소스 우선).
SOURCE_PRIORITY = ["broker", "value", "theme", "news", "momentum"]

STATE_REFRESHED_AT = "discovery_refreshed_at"
STATE_REFRESH_SUMMARY = "discovery_refresh_summary_json"

# 스케줄러와 수동 버튼이 같은 프로세스에서 동시에 수 분짜리 갱신을 돌리지 않도록
_refresh_lock = asyncio.Lock()


class RefreshInProgressError(RuntimeError):
    pass


def is_refreshing() -> bool:
    return _refresh_lock.locked()


_TRANSIENT_KIS_ERRORS = {"EGW00201", "EGW00316"}  # 초당 건수 초과 / "재 조회 수행 부탁드립니다"


async def _kis_retry(fn, *args, attempts: int = 3, **kwargs):
    """순위/뉴스 조회는 발굴 1회에 몇 번 안 되므로 일시 오류로 소스 하나를 통째로 잃지 않게 재시도한다."""
    for attempt in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except KisApiError as e:
            if e.msg_cd not in _TRANSIENT_KIS_ERRORS or attempt == attempts - 1:
                raise
            await asyncio.sleep(2 * (attempt + 1))


def eligible_universe(master: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        s: row for s, row in master.items()
        if not row["is_excluded"] and (row["market_cap_eok"] or 0) >= settings.DISCOVERY_MIN_MARKET_CAP_EOK
    }


async def source_momentum(client: AsyncKISClient) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for row in await _kis_retry(
        kis_domestic.get_fluctuation_rank, client, period_days=0, min_rise_pct=MOMENTUM_MIN_RISE_PCT,
    ):
        symbol = row.get("stck_shrn_iscd")
        if symbol:
            found[symbol] = {"rise_pct": float(row.get("prdy_ctrt") or 0)}
    for row in await _kis_retry(kis_domestic.get_volume_rank, client, blng_cls="1"):
        symbol = row.get("mksc_shrn_iscd")
        if not symbol:
            continue
        ev = found.setdefault(symbol, {"rise_pct": float(row.get("prdy_ctrt") or 0)})
        ev["vol_increase_pct"] = float(row.get("vol_inrt") or 0)
    return found


async def source_broker_research() -> Dict[str, Dict[str, Any]]:
    """리포트를 낸 증권사 수가 많고 많이 읽힌 순서로 정렬 (merge_sources가 이 순서로 자른다)."""
    reports = await asyncio.to_thread(fetch_company_research, 2)
    grouped = discovery_signals.group_research_reports(
        reports, settings.DISCOVERY_BROKER_LOOKBACK_DAYS, datetime.now(tz=KST),
    )

    def strength(ev: Dict[str, Any]) -> tuple:
        brokers = {r["broker"] for r in ev["reports"]}
        return (len(brokers), sum(r["read_count"] or 0 for r in ev["reports"]))

    return dict(sorted(grouped.items(), key=lambda item: strength(item[1]), reverse=True))


def count_headline_mentions(
    headlines: List[Dict[str, Any]], names: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """headlines: KIS news-title 행. 공시류는 iscd1~5에 종목코드가 있고, 일반 기사는 제목에
    종목명이 들어간 경우만 잡는다. 한 제목에서 '현대차'와 '현대차증권'이 같이 매칭되면 긴
    쪽만 인정한다."""
    counts: Dict[str, Dict[str, Any]] = {}
    name_items = [(n, s) for s, n in names.items() if len(n) >= NEWS_MIN_NAME_LEN]
    for h in headlines:
        title = h.get("hts_pbnt_titl_cntt") or ""
        symbols = {h.get(f"iscd{i}") for i in range(1, 6)} - {None, ""}
        matched = [n for n, _ in name_items if n in title]
        matched = [n for n in matched if not any(n != other and n in other for other in matched)]
        symbols |= {s for n, s in name_items if n in matched}
        for s in symbols:
            if s not in names:
                continue
            ev = counts.setdefault(s, {"mentions": 0, "titles": []})
            ev["mentions"] += 1
            if len(ev["titles"]) < 2:
                ev["titles"].append(title)
    return counts


async def source_news_buzz(client: AsyncKISClient, names: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """KIS 뉴스 제목 API는 한 번에 40건(수 분~수십 분 분량)만 주므로 최근 며칠의 여러 시각을
    표본으로 찍는다 — 전수 집계가 아니라 '표본에 자주 보이는 종목'이다."""
    headlines: List[Dict[str, Any]] = []
    seen: set = set()
    today = datetime.now(tz=KST)
    for day_offset in range(NEWS_SAMPLE_DAYS):
        date = (today - timedelta(days=day_offset)).strftime("%Y%m%d")
        for hour in NEWS_SAMPLE_HOURS:
            try:
                rows = await _kis_retry(kis_domestic.get_news_titles, client, date=date, hour=hour)
            except Exception:
                logger.warning(f"뉴스 헤드라인 표본 조회 실패 ({date} {hour})", exc_info=True)
                continue
            for row in rows:
                key = row.get("cntt_usiq_srno")
                if key and key not in seen:
                    seen.add(key)
                    headlines.append(row)
    counts = await asyncio.to_thread(count_headline_mentions, headlines, names)
    top = sorted(
        ((s, ev) for s, ev in counts.items() if ev["mentions"] >= NEWS_MIN_MENTIONS),
        key=lambda item: item[1]["mentions"], reverse=True,
    )[:NEWS_MAX_SYMBOLS]
    return {s: {**ev, "sampled_headlines": len(headlines)} for s, ev in top}


async def source_undervalued(eligible: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """KIS 호출 없이 네이버 컨센서스만 쓴다 (모의투자 1req/sec 예산을 점수 계산 단계에 남기기 위함).
    적자(PER<=0)이거나 ROE<=0인 종목은 '싼 게 아니라 이유가 있는' 경우가 많아 뺀다."""
    pool = sorted(eligible.values(), key=lambda r: r["market_cap_eok"] or 0, reverse=True)
    pool = pool[: settings.DISCOVERY_VALUE_POOL_SIZE]
    semaphore = asyncio.Semaphore(VALUE_FETCH_CONCURRENCY)

    async def fetch(row: Dict[str, Any]) -> Optional[tuple]:
        if (row.get("roe") or 0) <= 0:
            return None
        async with semaphore:
            consensus = await asyncio.to_thread(fetch_consensus, row["symbol"])
        target, close, per = consensus.get("target_price_mean"), consensus.get("last_close"), consensus.get("per")
        if not target or not close or not per or per <= 0:
            return None
        gap = (target - close) / close
        if gap < settings.DISCOVERY_VALUE_MIN_TARGET_GAP:
            return None
        return row["symbol"], {
            "target_gap_pct": round(gap, 3), "target_price": target, "per": per, "pbr": consensus.get("pbr"),
        }

    results = await asyncio.gather(*(fetch(row) for row in pool))
    hits = sorted((r for r in results if r), key=lambda item: item[1]["target_gap_pct"], reverse=True)
    return dict(hits[:VALUE_MAX_SYMBOLS])


async def source_theme_laggards(
    conn: aiosqlite.Connection, movers: Dict[str, float], eligible: Dict[str, Dict[str, Any]],
    names: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """급등 동료가 많은 테마의 후발주부터 정렬."""
    memberships = await db.get_theme_memberships(conn)
    laggards = discovery_signals.theme_laggards(memberships, movers, eligible, names)
    return dict(sorted(laggards.items(), key=lambda item: len(item[1]["movers"]), reverse=True))


def merge_sources(
    by_source: Dict[str, Dict[str, Dict[str, Any]]], eligible: Dict[str, Dict[str, Any]], limit: int,
) -> Dict[str, Dict[str, Any]]:
    """{source: {symbol: ev}} -> {symbol: {source: ev}}. 발굴 불가 종목(관리/정지/우선주/소형주 등)은
    여기서 걸러진다. 여러 소스에 동시에 잡힌 종목을 먼저 넣고, 남는 자리는 소스별로 번갈아
    (각 소스 안에서는 소스가 매긴 순서대로) 채운다 — 한 소스(예: 주 100건씩 나오는 증권사
    리포트)가 후보 풀을 독식하지 않게 하기 위함. by_source의 각 dict는 강한 순서로 정렬돼 있어야 한다."""
    merged: Dict[str, Dict[str, Any]] = {}
    for source, hits in by_source.items():
        for symbol, ev in hits.items():
            if symbol in eligible:
                merged.setdefault(symbol, {})[source] = ev

    multi = sorted((s for s, src in merged.items() if len(src) >= 2), key=lambda s: -len(merged[s]))
    chosen: List[str] = multi[:limit]
    taken = set(chosen)
    queues = {
        source: [s for s in by_source.get(source, {}) if s in merged and s not in taken]
        for source in SOURCE_PRIORITY
    }
    while len(chosen) < limit and any(queues.values()):
        for source in SOURCE_PRIORITY:
            queue = queues[source]
            while queue and queue[0] in taken:
                queue.pop(0)
            if queue and len(chosen) < limit:
                symbol = queue.pop(0)
                chosen.append(symbol)
                taken.add(symbol)
    return {s: merged[s] for s in chosen}


async def refresh_dynamic_universe(conn: aiosqlite.Connection, client: AsyncKISClient) -> Dict[str, Any]:
    if _refresh_lock.locked():
        raise RefreshInProgressError("종목 발굴 갱신이 이미 진행 중")
    async with _refresh_lock:
        return await _refresh(conn, client)


async def _refresh(conn: aiosqlite.Connection, client: AsyncKISClient) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"started_at": time.time(), "sources": {}, "errors": {}}

    master_ts = await db.get_stock_master_refreshed_at(conn)
    if master_ts is None or time.time() - master_ts > MASTER_MAX_AGE_SEC:
        try:
            summary["master"] = await master_files.refresh_stock_master(conn)
        except Exception as e:
            logger.exception("종목 마스터 갱신 실패 - 기존 마스터로 진행")
            summary["errors"]["master"] = str(e)
    master = await db.get_stock_master(conn)
    if not master:
        raise RuntimeError("종목 마스터가 비어 있어 동적 발굴을 할 수 없음 (마스터 파일 다운로드 실패)")
    eligible = eligible_universe(master)

    by_source: Dict[str, Dict[str, Dict[str, Any]]] = {}

    async def run(name: str, coro) -> None:
        try:
            by_source[name] = await coro
        except Exception as e:
            logger.exception(f"발굴 소스 '{name}' 실패 - 이 소스 없이 진행")
            summary["errors"][name] = str(e)
            by_source[name] = {}
        summary["sources"][name] = len(by_source[name])

    await run("momentum", source_momentum(client))
    await run("broker", source_broker_research())
    await run("news", source_news_buzz(client, {s: r["name"] for s, r in eligible.items()}))
    await run("value", source_undervalued(eligible))
    movers = {s: ev.get("rise_pct", 0) for s, ev in by_source["momentum"].items()
              if (ev.get("rise_pct") or 0) >= MOMENTUM_MIN_RISE_PCT}
    await run("theme", source_theme_laggards(conn, movers, eligible, {s: r["name"] for s, r in master.items()}))

    merged = merge_sources(by_source, eligible, settings.DISCOVERY_MAX_DYNAMIC)
    expires_at = time.time() + settings.DISCOVERY_DYNAMIC_TTL_DAYS * 86400
    for symbol, sources in merged.items():
        await db.upsert_dynamic_candidate(
            conn, symbol, eligible[symbol]["name"], json.dumps(sources, ensure_ascii=False), expires_at,
        )
    summary["upserted"] = len(merged)
    summary["expired"] = await db.disable_expired_candidates(conn, time.time())
    summary["expired"] += await db.trim_dynamic_candidates(conn, settings.DISCOVERY_MAX_DYNAMIC)

    benchmark_returns = await quant_metrics.fetch_benchmark_return_series(client)
    scored = await discovery.score_candidates(
        conn, client, benchmark_returns=benchmark_returns, broker_reports=by_source.get("broker"),
    )
    summary["scored"] = len(scored)
    summary["finished_at"] = time.time()

    await db.set_state(conn, STATE_REFRESHED_AT, str(summary["finished_at"]))
    await db.set_state(conn, STATE_REFRESH_SUMMARY, json.dumps(summary, ensure_ascii=False))
    logger.info(f"동적 종목 발굴 갱신 완료: {summary}")
    return summary
