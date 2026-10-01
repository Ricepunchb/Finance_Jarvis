# core/backtest/data.py
"""과거 OHLCV 데이터 수집 및 로컬 캐시 관리.

KIS OpenAPI를 통해 장기 일봉(OHLCV)을 청크 단위로 조회하고 SQLite(backtest_bars)에
영구 보관합니다. 한 번 캐시된 과거 일자는 불변이므로 추가 API 호출 없이 로컬에서
즉시 로드되어 초고속 백테스트가 가능합니다.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite
import pandas as pd

from core import db, indicators, kis_domestic, kis_overseas
from core.config import settings
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

DOMESTIC_CHUNK_DAYS = 120  # KIS 일봉 1회 최대 ~100영업일
LOOKBACK_PADDING_DAYS = 60  # 기술적 지표 콜드스타트(RSI 14, MACD 26 등)를 위한 사전 일봉 확보일


class HistoricalDataManager:
    def __init__(self, conn: aiosqlite.Connection, client: Optional[AsyncKISClient] = None):
        self.conn = conn
        self.client = client

    async def get_ohlcv(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        market: str = "domestic",
        exchange: Optional[str] = None,
        include_padding: bool = True,
    ) -> pd.DataFrame:
        """symbol의 OHLCV DataFrame을 조회. 캐시에 없으면 KIS에서 수집 후 반환."""
        fetch_start = start_date
        if include_padding:
            dt_start = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=LOOKBACK_PADDING_DAYS)
            fetch_start = dt_start.strftime("%Y-%m-%d")

        # 1. 로컬 캐시 확인
        cached = await db.get_backtest_bars(self.conn, symbol, market, fetch_start, end_date)
        cached_dates = {row["date"] for row in cached}

        # 2. 누락 구간 파악 및 필요 시 KIS 증분 수집
        need_fetch = False
        if not cached:
            need_fetch = True
        else:
            first_cached = min(cached_dates)
            last_cached = max(cached_dates)
            if first_cached > fetch_start or last_cached < end_date:
                need_fetch = True

        if need_fetch and self.client is not None:
            await self._fetch_and_cache(symbol, market, exchange, fetch_start, end_date)
            cached = await db.get_backtest_bars(self.conn, symbol, market, fetch_start, end_date)

        if not cached:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(cached)
        df["date"] = pd.to_datetime(df["date"])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return df

    async def _fetch_and_cache(
        self,
        symbol: str,
        market: str,
        exchange: Optional[str],
        start_date: str,
        end_date: str,
    ) -> None:
        """KIS API를 호출하여 구간 전체의 OHLCV를 수집하고 DB에 저장."""
        if self.client is None:
            logger.warning("KIS 클라이언트가 없어 과거 데이터를 조회할 수 없습니다: %s", symbol)
            return

        bars: List[Dict[str, Any]] = []
        try:
            if market == "domestic":
                bars = await self._fetch_domestic_chunks(symbol, start_date, end_date)
            elif market == "overseas" and exchange:
                bars = await self._fetch_overseas_chunks(exchange, symbol, start_date, end_date)
        except Exception as e:
            logger.exception("과거 데이터 수집 실패 %s (%s): %s", symbol, market, e)
            return

        if bars:
            await db.save_backtest_bars(self.conn, symbol, market, bars)
            logger.info("과거 일봉 적재 완료: %s (%d건, %s ~ %s)", symbol, len(bars), start_date, end_date)

    async def _fetch_domestic_chunks(
        self, symbol: str, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        limit_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        limit_end = datetime.strptime(end_date, "%Y-%m-%d").date()

        bars_dict: Dict[str, Dict[str, Any]] = {}
        cursor = limit_end

        while cursor >= limit_start:
            chunk_start = max(limit_start, cursor - timedelta(days=DOMESTIC_CHUNK_DAYS))
            s_str = chunk_start.strftime("%Y%m%d")
            e_str = cursor.strftime("%Y%m%d")

            rows = await kis_domestic.get_daily_chart(self.client, symbol, s_str, e_str)
            df = indicators.chart_rows_to_dataframe(rows)

            if df.empty:
                # 데이터가 더 이상 없으면 중단
                break

            for _, r in df.iterrows():
                d_str = r["date"].strftime("%Y-%m-%d")
                if limit_start <= r["date"].date() <= limit_end:
                    bars_dict[d_str] = {
                        "date": d_str,
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["volume"]),
                    }

            oldest_in_chunk = df["date"].min().date()
            if oldest_in_chunk <= limit_start or oldest_in_chunk >= cursor:
                break
            cursor = chunk_start - timedelta(days=1)

        return sorted(bars_dict.values(), key=lambda x: x["date"])

    async def _fetch_overseas_chunks(
        self, exchange: str, symbol: str, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        limit_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        limit_end = datetime.strptime(end_date, "%Y-%m-%d").date()

        bars_dict: Dict[str, Dict[str, Any]] = {}
        base_date = limit_end.strftime("%Y%m%d")

        for _ in range(10):  # 최대 10페이지
            rows = await kis_overseas.get_daily_chart(self.client, exchange, symbol, base_date=base_date)
            df = indicators.chart_rows_to_dataframe_overseas(rows)
            if df.empty:
                break

            for _, r in df.iterrows():
                d_str = r["date"].strftime("%Y-%m-%d")
                if limit_start <= r["date"].date() <= limit_end:
                    bars_dict[d_str] = {
                        "date": d_str,
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r["volume"]),
                    }

            oldest_in_chunk = df["date"].min().date()
            if oldest_in_chunk <= limit_start:
                break
            base_date = (oldest_in_chunk - timedelta(days=1)).strftime("%Y%m%d")

        return sorted(bars_dict.values(), key=lambda x: x["date"])
