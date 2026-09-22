# core/intraday.py
"""30분봉 기반 스윙 시그널 오케스트레이션.

KIS는 국내는 1분봉만(최대 120건/회, 날짜별), 해외는 30분봉을 직접(NMIN=30) 준다.
이 모듈은 그 차이를 흡수해서 양쪽 다 core.indicators.compute_technical_signal()이
바로 먹을 수 있는 (date/open/high/low/close/volume) DataFrame으로 맞춰준다 — 지표
계산 자체는 손대지 않고 그대로 재사용한다.

캐시(core.db.intraday_bars)에 쌓인 분량이 MIN_BARS_REQUIRED에 못 미치면 과거 데이터를
백필하는데, 모의투자 초당 1건 제한 때문에 한 사이클에 전 종목을 한꺼번에 백필하면
그 사이클이 통째로 지연된다. 그래서 백필은 사이클당 종목 수를 제한해서 여러 사이클에
걸쳐 분산한다 — 아직 백필 안 된 종목은 이번 사이클엔 그냥 HOLD로 넘어간다
(compute_technical_signal이 이미 갖고 있는 콜드스타트 동작 그대로).
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pandas as pd

from core import db, indicators, kis_domestic, kis_overseas
from core.config import settings
from core.kis_client import AsyncKISClient
from core.kis_common import KisApiError

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
Signal = Dict[str, Any]

_KRX_OPEN_HOUR_1 = "090000"
_KRX_CLOSE_HOUR_1 = "153000"
_MAX_PAGES_PER_DAY = 6  # 390분/거래일 ÷ 120건/회 ≈ 4회 + 여유


def _domestic_rows_to_1min_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "stck_oprc": "open", "stck_hgpr": "high", "stck_lwpr": "low",
        "stck_prpr": "close", "cntg_vol": "volume",
    })
    df["date"] = pd.to_datetime(
        df["stck_bsop_date"] + df["stck_cntg_hour"], format="%Y%m%d%H%M%S"
    ).dt.tz_localize(KST)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)
    return df


def _overseas_rows_to_30min_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df = df.rename(columns={"last": "close", "evol": "volume"})
    # kymd/khms(한국시각 기준)를 써서 09:00 KST 앵커 리샘플링과 타임존 혼선 없이 맞춘다.
    df["date"] = pd.to_datetime(df["kymd"] + df["khms"], format="%Y%m%d%H%M%S").dt.tz_localize(KST)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)
    return df


def _resample_to_30min(df_1min: pd.DataFrame) -> pd.DataFrame:
    """1분봉 -> 09:00 KST 앵커 30분봉. 아직 다 안 찬 마지막 버킷은 버린다."""
    if df_1min.empty:
        return df_1min
    indexed = df_1min.set_index("date")
    resampled = indexed.resample("30min", origin="start_day", offset="9h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    if resampled.empty:
        return resampled.reset_index()
    last_bucket_start = resampled.index[-1]
    last_bucket_end = last_bucket_start + timedelta(minutes=30)
    if df_1min["date"].max() < last_bucket_end - timedelta(minutes=1):
        resampled = resampled.iloc[:-1]
    return resampled.reset_index()


def _bars_to_cache_rows(df_30min: pd.DataFrame, symbol: str, market: str) -> List[Dict[str, Any]]:
    return [
        {
            "symbol": symbol, "market": market, "bar_start": row["date"].timestamp(),
            "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"],
        }
        for _, row in df_30min.iterrows()
    ]


def _cached_rows_to_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["bar_start"], unit="s", utc=True).dt.tz_convert(KST)
    return df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)


def _trading_days_back(n: int) -> List[str]:
    """오늘 포함, 최근 영업일(주말만 제외 — 공휴일 캘린더는 없음) n일을 YYYYMMDD로."""
    days = []
    cursor = datetime.now(tz=KST)
    while len(days) < n:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return days


async def _fetch_historical_page_with_retry(
    client: AsyncKISClient, symbol: str, date_str: str, hour_1: str, retries: int = 2,
) -> List[Dict[str, Any]]:
    """모의투자 실측 결과, 클라이언트의 초당 1건 세마포어(정확히 1.0초 간격)로도 KIS가
    간헐적으로 EGW00201(초당 거래건수 초과)을 낼 수 있음을 확인했다 — 짧은 백오프 후
    재시도해 분봉 백필 전체가 단 한 번의 일시적 rate-limit 때문에 통째로 실패하지 않게 한다."""
    for attempt in range(retries + 1):
        try:
            return await kis_domestic.get_minute_chart_historical(client, symbol, date_str, hour_1)
        except KisApiError as e:
            if e.msg_cd != "EGW00201" or attempt == retries:
                raise
            logger.warning(f"'{symbol}' 분봉조회 rate-limit({e.msg_cd}) - {attempt + 1}번째 재시도 전 대기")
            await asyncio.sleep(2.0)
    return []


async def _backfill_domestic(client: AsyncKISClient, conn, symbol: str) -> None:
    lookback_trading_days = max(3, settings.INTRADAY_LOOKBACK_CALENDAR_DAYS * 5 // 7)
    all_rows: List[Dict[str, Any]] = []
    for date_str in _trading_days_back(lookback_trading_days):
        hour_1 = _KRX_CLOSE_HOUR_1
        for _ in range(_MAX_PAGES_PER_DAY):
            page = await _fetch_historical_page_with_retry(client, symbol, date_str, hour_1)
            if not page:
                break
            all_rows.extend(page)
            earliest = min(page, key=lambda r: r["stck_cntg_hour"])["stck_cntg_hour"]
            if earliest <= _KRX_OPEN_HOUR_1:
                break
            # 다음 페이지는 이번 페이지 최소시각 1분 전부터
            earliest_dt = datetime.strptime(earliest, "%H%M%S")
            hour_1 = (earliest_dt - timedelta(minutes=1)).strftime("%H%M%S")
    df_1min = _domestic_rows_to_1min_df(all_rows)
    df_30min = _resample_to_30min(df_1min)
    if not df_30min.empty:
        await db.save_intraday_bars(conn, symbol, "domestic", _bars_to_cache_rows(df_30min, symbol, "domestic"))


async def _backfill_overseas(client: AsyncKISClient, conn, symbol: str, exchange: str) -> None:
    rows = await kis_overseas.get_minute_chart(client, exchange, symbol, n_min="30", include_prev_day=True)
    df_30min = _overseas_rows_to_30min_df(rows)
    if not df_30min.empty:
        await db.save_intraday_bars(conn, symbol, "overseas", _bars_to_cache_rows(df_30min, symbol, "overseas"))


async def get_intraday_signal_domestic(
    client: AsyncKISClient, conn, symbol: str, allow_backfill: bool,
) -> Signal:
    since_ts = (datetime.now(tz=KST) - timedelta(days=settings.INTRADAY_LOOKBACK_CALENDAR_DAYS)).timestamp()
    cached = await db.get_cached_intraday_bars(conn, symbol, "domestic", since_ts)
    if len(cached) < indicators.MIN_BARS_REQUIRED:
        if not allow_backfill:
            return {"direction": "HOLD", "strength": 0.0, "detail": "분봉 백필 대기 중"}
        try:
            await _backfill_domestic(client, conn, symbol)
        except Exception:
            logger.exception(f"'{symbol}' 국내 분봉 백필 실패 - 이번 사이클은 HOLD")
            return {"direction": "HOLD", "strength": 0.0, "detail": "분봉 백필 실패"}
        cached = await db.get_cached_intraday_bars(conn, symbol, "domestic", since_ts)

    # 최근 30분(당일분봉)만 증분 갱신 — 매 사이클 저비용 호출
    try:
        now = datetime.now(tz=KST)
        recent_rows = await kis_domestic.get_minute_chart_today(client, symbol, now.strftime("%H%M%S"))
        df_1min = _domestic_rows_to_1min_df(recent_rows)
        df_30min = _resample_to_30min(df_1min)
        if not df_30min.empty:
            await db.save_intraday_bars(conn, symbol, "domestic", _bars_to_cache_rows(df_30min, symbol, "domestic"))
            cached = await db.get_cached_intraday_bars(conn, symbol, "domestic", since_ts)
    except Exception:
        logger.warning(f"'{symbol}' 당일분봉 증분 갱신 실패 - 캐시된 데이터로 계속", exc_info=True)

    return indicators.compute_technical_signal(_cached_rows_to_df(cached))


async def get_intraday_signal_overseas(
    client: AsyncKISClient, conn, symbol: str, exchange: str, allow_backfill: bool,
) -> Signal:
    since_ts = (datetime.now(tz=KST) - timedelta(days=settings.INTRADAY_LOOKBACK_CALENDAR_DAYS)).timestamp()
    cached = await db.get_cached_intraday_bars(conn, symbol, "overseas", since_ts)
    if len(cached) < indicators.MIN_BARS_REQUIRED:
        if not allow_backfill:
            return {"direction": "HOLD", "strength": 0.0, "detail": "분봉 백필 대기 중"}
        try:
            await _backfill_overseas(client, conn, symbol, exchange)
        except Exception:
            logger.exception(f"'{symbol}' 해외 분봉 백필 실패 - 이번 사이클은 HOLD")
            return {"direction": "HOLD", "strength": 0.0, "detail": "분봉 백필 실패"}
        cached = await db.get_cached_intraday_bars(conn, symbol, "overseas", since_ts)
    else:
        # 30분 네이티브라 저비용 — 매 사이클 최신 페이지 한 번으로 증분 갱신
        try:
            rows = await kis_overseas.get_minute_chart(client, exchange, symbol, n_min="30", include_prev_day=True)
            df_30min = _overseas_rows_to_30min_df(rows)
            if not df_30min.empty:
                await db.save_intraday_bars(conn, symbol, "overseas", _bars_to_cache_rows(df_30min, symbol, "overseas"))
                cached = await db.get_cached_intraday_bars(conn, symbol, "overseas", since_ts)
        except Exception:
            logger.warning(f"'{symbol}' 해외 분봉 증분 갱신 실패 - 캐시된 데이터로 계속", exc_info=True)

    return indicators.compute_technical_signal(_cached_rows_to_df(cached))
