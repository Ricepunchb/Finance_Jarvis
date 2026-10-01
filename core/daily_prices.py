# core/daily_prices.py
"""분석 대시보드용 일봉 종가 캐시 적재.

KIS 일봉은 engine.client로만 안전하게 조회할 수 있다(별도 클라이언트를 만들면 토큰 재발급 + 카톡 알림).
그래서 이 모듈은 FastAPI 프로세스 안에서 호출되고, Streamlit은 결과만 /analytics/* 로 받는다.

과거 일자는 불변이므로 한 번 받으면 다시 조회하지 않고, 마지막 일자가 오늘보다 오래됐을 때만 갱신한다.
KIS 조회가 실패하면(모의 환경의 해외 일봉 미지원 등) intraday_bars 캐시를 일 단위로 접어 폴백한다.
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite

from core import db, indicators, kis_domestic, kis_overseas
from core.config import settings
from core.kis_client import AsyncKISClient
from core.pnl import KST, local_date

logger = logging.getLogger(__name__)

REFRESH_TTL_SEC = 600          # 같은 종목을 10분 안에 다시 KIS에 묻지 않는다 (프로세스 메모리)
DOMESTIC_CHUNK_DAYS = 120      # KIS 일봉은 호출당 약 100행 -> 달력일 120일(약 85거래일)씩 끊어 조회
OVERSEAS_MAX_PAGES = 6
_last_fetch: Dict[str, float] = {}


def _fmt(date_str: str) -> str:
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def _today_kst() -> datetime:
    return datetime.now(KST)


async def _fetch_domestic(client: AsyncKISClient, symbol: str, since: str) -> List[Dict[str, Any]]:
    end = _today_kst().date()
    start_limit = datetime.strptime(since, "%Y-%m-%d").date()
    bars: List[Dict[str, Any]] = []
    cursor = end
    while cursor >= start_limit:
        chunk_start = max(start_limit, cursor - timedelta(days=DOMESTIC_CHUNK_DAYS))
        rows = await kis_domestic.get_daily_chart(
            client, symbol, chunk_start.strftime("%Y%m%d"), cursor.strftime("%Y%m%d")
        )
        df = indicators.chart_rows_to_dataframe(rows)
        if not df.empty:
            bars.extend({"date": d.strftime("%Y-%m-%d"), "close": float(c)}
                        for d, c in zip(df["date"], df["close"]) if c == c and c > 0)
        cursor = chunk_start - timedelta(days=1)
    return bars


async def _fetch_overseas(client: AsyncKISClient, exchange: str, symbol: str, since: str) -> List[Dict[str, Any]]:
    """해외 일봉은 base_date(BYMD)를 커서로 과거로 페이징한다."""
    bars: List[Dict[str, Any]] = []
    base_date = ""
    for _ in range(OVERSEAS_MAX_PAGES):
        rows = await kis_overseas.get_daily_chart(client, exchange, symbol, base_date=base_date)
        df = indicators.chart_rows_to_dataframe_overseas(rows)
        if df.empty:
            break
        bars.extend({"date": d.strftime("%Y-%m-%d"), "close": float(c)} for d, c in zip(df["date"], df["close"])
                    if c == c and c > 0)
        oldest = df["date"].min()
        if oldest.strftime("%Y-%m-%d") <= since:
            break
        base_date = (oldest - timedelta(days=1)).strftime("%Y%m%d")
    return bars


async def daily_from_intraday(
    conn: aiosqlite.Connection, symbol: str, market: str, since: str
) -> List[Dict[str, Any]]:
    """30분봉 캐시를 거래일별 마지막 종가로 접는다 (KIS 일봉 조회 실패 시 폴백)."""
    since_ts = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=KST).timestamp() - 86400
    bars = await db.get_cached_intraday_bars(conn, symbol, market, since_ts)
    last: Dict[str, float] = {}
    for bar in bars:  # bar_start 오름차순이므로 마지막 값이 그날 종가
        last[local_date(market, bar["bar_start"])] = float(bar["close"])
    return [{"date": d, "close": c} for d, c in sorted(last.items()) if d >= since]


async def ensure_daily_bars(
    conn: aiosqlite.Connection,
    client: Optional[AsyncKISClient],
    symbols: Dict[str, Dict[str, Any]],
    since: str,
    force: bool = False,
) -> Dict[str, str]:
    """symbols: {symbol: {"market": ..., "exchange": ...}}.  since: 'YYYY-MM-DD'.

    종목별 상태를 반환: 'cached'(새로 안 받음) | 'fetched' | 'fallback_intraday' | 'stale'(조회 실패, 기존 캐시 사용) | 'failed'.
    """
    status: Dict[str, str] = {}
    for symbol, info in symbols.items():
        market = info.get("market") or "domestic"
        stored = await db.get_daily_bars(conn, symbol, market, since)
        recently_fetched = time.time() - _last_fetch.get(symbol, 0.0) < REFRESH_TTL_SEC
        if not force and stored and recently_fetched:
            status[symbol] = "cached"
            continue
        # 이력이 since부터 이어져 있으면 마지막 저장일 근처만 증분 조회한다 (과거 일자는 불변)
        covers_since = bool(stored) and stored[0]["date"] <= (
            datetime.strptime(since, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
        fetch_since = since
        if covers_since and not force:
            fetch_since = (datetime.strptime(stored[-1]["date"], "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")

        bars: List[Dict[str, Any]] = []
        state = "failed"
        skip_kis = market == "overseas" and settings.DOMESTIC_ONLY  # 국내 전용: 해외 KIS 시세는 호출하지 않는다
        if client is not None and not skip_kis:
            try:
                if market == "overseas":
                    if not info.get("exchange"):
                        raise RuntimeError("해외 종목의 거래소 코드 없음")
                    bars = await _fetch_overseas(client, info["exchange"], symbol, fetch_since)
                else:
                    bars = await _fetch_domestic(client, symbol, fetch_since)
                state = "fetched"
            except Exception as exc:  # noqa: BLE001 — KIS 오류/미지원 시 폴백이 목적
                logger.warning("일봉 조회 실패 %s(%s): %s", symbol, market, exc)
                bars = []
        if not bars:
            bars = await daily_from_intraday(conn, symbol, market, fetch_since)
            if bars:
                state = "fallback_intraday"
        if bars:
            await db.save_daily_bars(conn, symbol, market, bars)
        _last_fetch[symbol] = time.time()
        status[symbol] = state if bars else ("stale" if stored else "failed")
    return status


async def load_closes(
    conn: aiosqlite.Connection, symbols: Dict[str, Dict[str, Any]], since: str
) -> Dict[str, Dict[str, float]]:
    """{symbol: {date: close}} — 캐시 테이블에서만 읽는다(네트워크 없음)."""
    out: Dict[str, Dict[str, float]] = {}
    for symbol, info in symbols.items():
        rows = await db.get_daily_bars(conn, symbol, info.get("market") or "domestic", since)
        out[symbol] = {r["date"]: r["close"] for r in rows}
    return out
