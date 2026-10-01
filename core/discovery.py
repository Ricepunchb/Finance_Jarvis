# core/discovery.py
"""AI 포트폴리오 에이전트의 종목 발굴. LLM은 candidate_universe 테이블 안에서만 신규
편입을 제안할 수 있다 — 이 테이블 밖의 종목코드는 존재하지 않는 것으로 간주한다.
테이블은 시드/수동 후보 + core/discovery_sources.py가 매일 채우는 동적 후보로 구성된다.

국내 종목만 지원한다(Phase 5.1 범위) — 해외는 시장시간/환율/거래소별 API가 달라
스크리닝 비용이 크게 늘어나므로 의도적으로 미룸.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite
import pandas as pd

from core import db, discovery_signals, indicators, kis_domestic, quant_metrics
from core.config import settings
from core.fundamentals.naver_research import fetch_company_research
from core.fundamentals.valuation import get_valuation_signal
from core.kis_client import AsyncKISClient
from core.news.naver_finance import fetch_recent_news

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

CHART_LOOKBACK_DAYS = 90
NEWS_FETCH_COUNT = 50
DEFAULT_SEED_PATH = Path("data/candidate_universe_seed.json")
STATE_SCORED_CACHE = "discovery_scored_json"


@dataclass
class ValidationOutcome:
    ok: bool
    reason: str = ""
    kis_name: Optional[str] = None


async def validate_candidate_symbol(
    client: AsyncKISClient,
    symbol: str,
    market: str = "domestic",
    claimed_name: Optional[str] = None,
) -> ValidationOutcome:
    """후보가 candidate_universe에 들어가거나 ADD로 실제 채택되기 전 반드시 통과해야 한다.

    1) KIS 실재성 확인 (존재하지 않는/오타 종목코드 차단) - 가격이 정상 조회되면 실재로 간주
    2) VI(거래정지) 상태 확인
    3) LLM이 주장한 종목명과 KIS 실제 종목명 대조 (코드가 우연히 존재해도 이름이 다르면
       환각 의심으로 반려) - 단, 모의투자 시세조회는 종목명(hts_kor_isnm)을 아예 비워서
       주는 경우가 있어 그럴 땐 이름 대조를 건너뛰고 가격 조회 성공만으로 통과시킨다
       (그렇지 않으면 실전 종목코드가 모의투자에서 전부 반려되는 사고가 남).
    """
    if market != "domestic":
        return ValidationOutcome(False, "해외 후보종목은 아직 지원하지 않음 (Phase 5.1 범위 밖)")

    try:
        price_info = await kis_domestic.get_price(client, symbol)
    except Exception:
        return ValidationOutcome(False, "KIS 시세조회 실패 (존재하지 않는 종목코드일 가능성)")

    price = float(price_info.get("stck_prpr") or 0)
    if price <= 0:
        return ValidationOutcome(False, "현재가 조회 실패 (존재하지 않는 종목코드일 가능성)")

    kis_name = price_info.get("hts_kor_isnm")
    if not kis_name:
        logger.warning(f"'{symbol}' KIS 응답에 종목명 없음(모의투자 API 제약으로 추정) - 이름 대조 없이 가격만으로 실재성 인정")

    try:
        vi_rows = await kis_domestic.get_vi_status(client, symbol)
        if vi_rows:
            return ValidationOutcome(False, "VI(거래정지) 발동 중 - 편입 후보에서 제외")
    except Exception:
        logger.exception(f"'{symbol}' VI 상태 조회 실패 - 검증은 계속 진행")

    if kis_name and claimed_name and claimed_name.strip():
        claimed = claimed_name.strip()
        if claimed not in kis_name and kis_name not in claimed:
            return ValidationOutcome(
                False,
                f"종목명 불일치 (제안: '{claimed}', KIS 실제: '{kis_name}') - 환각 의심",
            )

    return ValidationOutcome(True, kis_name=kis_name)


async def seed_candidate_universe(
    conn: aiosqlite.Connection, client: AsyncKISClient, seed_path: Path = DEFAULT_SEED_PATH,
) -> Dict[str, int]:
    """seed_path의 JSON([{"symbol","name","universe_tag"}, ...])을 KIS 검증 후
    candidate_universe에 적재한다. 실재하지 않는 항목은 조용히 버리지 않고 반려 사유를
    로그로 남긴다."""
    if not seed_path.exists():
        return {"added": 0, "rejected": 0, "skipped": 0}

    entries = json.loads(seed_path.read_text(encoding="utf-8"))
    existing = {row["symbol"] for row in await db.list_candidate_universe(conn)}

    added = rejected = skipped = 0
    for entry in entries:
        symbol = entry["symbol"]
        if symbol in existing:
            skipped += 1
            continue
        outcome = await validate_candidate_symbol(client, symbol, claimed_name=entry.get("name"))
        if not outcome.ok:
            logger.warning(f"후보종목 시딩 반려: {symbol} ({entry.get('name', '')}) - {outcome.reason}")
            rejected += 1
            continue
        await db.add_candidate_symbol(
            conn, symbol, name=outcome.kis_name or entry.get("name", ""),
            universe_tag=entry.get("universe_tag", "KOSPI_LARGE_CAP"),
        )
        added += 1
    return {"added": added, "rejected": rejected, "skipped": skipped}


async def _score_one(
    conn: aiosqlite.Connection,
    client: AsyncKISClient,
    candidate: Dict[str, Any],
    broker_reports: Dict[str, Dict[str, Any]],
    benchmark_returns: Optional[pd.Series],
    start_date: str,
    end_date: str,
) -> Optional[Dict[str, Any]]:
    symbol = candidate["symbol"]
    try:
        chart_rows = await kis_domestic.get_daily_chart(client, symbol, start_date, end_date)
        df = indicators.chart_rows_to_dataframe(chart_rows)
        tech_signal = indicators.compute_technical_signal(df)
    except Exception:
        logger.exception(f"'{symbol}' 스크리닝 시그널 계산 실패 - 이번 스크리닝에서 제외")
        return None

    sources = json.loads(candidate.get("sources_json") or "{}")
    if symbol in broker_reports:
        sources["broker"] = broker_reports[symbol]

    entry: Dict[str, Any] = {
        "symbol": symbol, "name": candidate["name"], "universe_tag": candidate["universe_tag"],
        "tech": tech_signal, "sources": sources,
    }
    entry.update(quant_metrics.compute_price_based_metrics(df, benchmark_returns))
    entry.update(indicators.compute_technical_detail(df))
    entry.update(discovery_signals.volume_accumulation(df))

    value_strength = 0.0
    valuation = await get_valuation_signal(conn, client, symbol)
    if valuation and valuation["direction"] == "BUY":
        value_strength = valuation["strength"]
    cached = await db.get_cached_valuation(conn, symbol, settings.VALUATION_CACHE_TTL_HOURS)
    last_close = float(df["close"].iloc[-1]) if not df.empty else 0.0
    if cached:
        entry["per"], entry["pbr"] = cached.get("per"), cached.get("pbr")
        target = cached.get("target_price_mean")
        if target and last_close > 0:
            entry["target_gap_pct"] = round((target - last_close) / last_close, 3)

    articles = await asyncio.to_thread(fetch_recent_news, symbol, NEWS_FETCH_COUNT)
    entry.update(discovery_signals.news_acceleration(
        [a["published_at"] for a in articles], time.time(), truncated=len(articles) >= NEWS_FETCH_COUNT,
    ))

    if "theme" in sources:
        entry["theme_laggard"] = sources["theme"]
        entry["theme"] = sources["theme"]["theme"]

    early, early_reasons = discovery_signals.early_signal(entry)
    entry["raw_components"] = {
        "tech": round(tech_signal["strength"], 3) if tech_signal["direction"] == "BUY" else 0.0,
        "value": round(value_strength, 3),
        "early": round(early, 3),
        "broker": round(discovery_signals.broker_score((sources.get("broker") or {}).get("reports", [])), 3),
    }
    entry["thesis"] = discovery_signals.build_thesis(entry, early_reasons)
    return entry


async def score_candidates(
    conn: aiosqlite.Connection,
    client: AsyncKISClient,
    benchmark_returns: Optional[pd.Series] = None,
    broker_reports: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """candidate_universe 전체(시드+수동+동적)를 점수 매겨 캐시에 저장한다. 모의투자 1req/sec에서
    종목당 KIS 2~4회(일봉 + 밸류에이션 캐시 미스 시 재무비율/2년 일봉)라 수십 종목이면 수 분
    걸린다 — 평소에는 하루 1회 갱신 작업에서만 돌고, screen_candidates는 캐시를 읽는다."""
    candidates = await db.list_candidate_universe(conn)
    if broker_reports is None:
        reports = await asyncio.to_thread(fetch_company_research, 2)
        broker_reports = discovery_signals.group_research_reports(
            reports, settings.DISCOVERY_BROKER_LOOKBACK_DAYS, datetime.now(tz=KST),
        )

    today = datetime.now(tz=KST)
    start_date = (today - timedelta(days=CHART_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    scored = []
    for c in candidates:
        entry = await _score_one(conn, client, c, broker_reports, benchmark_returns, start_date, end_date)
        if entry is not None:
            scored.append(entry)
    discovery_signals.finalize_scores(scored, settings.DISCOVERY_WEIGHTS)

    await db.set_state(
        conn, STATE_SCORED_CACHE,
        json.dumps({"scored_at": time.time(), "entries": scored}, ensure_ascii=False, default=str),
    )
    return scored


async def load_scored_cache(conn: aiosqlite.Connection) -> Optional[Dict[str, Any]]:
    raw = await db.get_state(conn, STATE_SCORED_CACHE)
    return json.loads(raw) if raw else None


async def screen_candidates(
    conn: aiosqlite.Connection,
    client: AsyncKISClient,
    exclude_symbols: List[str],
    benchmark_returns: Optional[pd.Series] = None,
) -> List[Dict[str, Any]]:
    """아직 보유하지 않은 후보 중 관점별 할당(저평가/조기신호/모멘텀/증권사)을 지켜 상위
    DISCOVERY_TOP_N개를 LLM 숏리스트로 반환한다. 점수 캐시가 신선하면 KIS 호출 없이 캐시를
    쓰고, 없거나 오래됐으면 그 자리에서 다시 계산한다."""
    cache = await load_scored_cache(conn)
    fresh = cache is not None and (time.time() - cache["scored_at"]) < settings.DISCOVERY_SHORTLIST_TTL_HOURS * 3600
    if fresh:
        enabled = {c["symbol"] for c in await db.list_candidate_universe(conn)}
        entries = [e for e in cache["entries"] if e["symbol"] in enabled]
    else:
        entries = await score_candidates(conn, client, benchmark_returns=benchmark_returns)
    return discovery_signals.select_with_quota(
        entries, settings.DISCOVERY_TOP_N, settings.DISCOVERY_ANGLE_QUOTA, exclude=set(exclude_symbols),
        min_score=settings.DISCOVERY_MIN_SCORE,
    )
