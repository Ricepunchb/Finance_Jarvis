# core/discovery.py
"""AI 포트폴리오 에이전트의 종목 발굴. LLM은 candidate_universe 테이블 안에서만 신규
편입을 제안할 수 있다 — 이 테이블 밖의 종목코드는 존재하지 않는 것으로 간주한다.

국내 종목만 지원한다(Phase 5.1 범위) — 해외는 시장시간/환율/거래소별 API가 달라
스크리닝 비용이 크게 늘어나므로 의도적으로 미룸.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite
import pandas as pd

from core import db, indicators, kis_domestic, quant_metrics
from core.config import settings
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

CHART_LOOKBACK_DAYS = 90
DEFAULT_SEED_PATH = Path("data/candidate_universe_seed.json")


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


async def screen_candidates(
    conn: aiosqlite.Connection,
    client: AsyncKISClient,
    exclude_symbols: List[str],
    benchmark_returns: Optional[pd.Series] = None,
) -> List[Dict[str, Any]]:
    """candidate_universe 중 아직 보유하지 않은 종목을 저비용 기술적 시그널로 스코어링해
    상위 DISCOVERY_TOP_N개만 LLM 숏리스트로 반환한다 (뉴스감성 등 비싼 시그널은 여기서 안 씀).

    이미 조회한 일봉 df로 성과/리스크 지표(MDD/변동성/샤프/베타 등)도 같이 계산해 붙인다 -
    같은 chart_rows를 재사용하므로 추가 KIS 호출은 없다."""
    candidates = await db.list_candidate_universe(conn)
    exclude = set(exclude_symbols)
    pool = [c for c in candidates if c["symbol"] not in exclude]
    if not pool:
        return []

    today = datetime.now(tz=KST)
    start_date = (today - timedelta(days=CHART_LOOKBACK_DAYS)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    scored = []
    for c in pool:
        try:
            chart_rows = await kis_domestic.get_daily_chart(client, c["symbol"], start_date, end_date)
            df = indicators.chart_rows_to_dataframe(chart_rows)
            tech_signal = indicators.compute_technical_signal(df)
        except Exception:
            logger.exception(f"'{c['symbol']}' 스크리닝 시그널 계산 실패 - 이번 스크리닝에서 제외")
            continue
        score = tech_signal["strength"] if tech_signal["direction"] == "BUY" else 0.0
        entry = {"symbol": c["symbol"], "name": c["name"], "tech": tech_signal, "_score": score}
        entry.update(quant_metrics.compute_price_based_metrics(df, benchmark_returns))
        entry.update(indicators.compute_technical_detail(df))
        scored.append(entry)

    scored.sort(key=lambda item: item["_score"], reverse=True)
    top = scored[: settings.DISCOVERY_TOP_N]
    for item in top:
        item.pop("_score", None)
    return top
