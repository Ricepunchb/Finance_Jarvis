# core/news/naver_finance.py
"""네이버페이 증권(m.stock.naver.com)의 종목별 뉴스 JSON API를 사용한 단일 뉴스 소스.

Phase 2는 파이프라인(수집->캐시->LLM 감성분석->시그널 결합) 검증이 목적이므로
소스는 하나만 쓴다. 이 API가 언젠가 바뀌면(실제로 finance.naver.com의 구버전
뉴스 페이지는 이미 폐기되어 이 API로 교체된 바 있다) 이 파일만 교체하면 된다.
"""
import html
import logging
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import requests

from core.fundamentals import yfinance_client
from core.utils import symbol_mapper

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
API_URL = "https://m.stock.naver.com/api/news/stock/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT_SEC = 10


def _parse_datetime(raw: str) -> float:
    """'YYYYMMDDHHMM' -> epoch(KST 기준)."""
    dt = datetime.strptime(raw, "%Y%m%d%H%M").replace(tzinfo=KST)
    return dt.timestamp()


def fetch_recent_news(symbol: str, max_items: int = 5) -> List[Dict[str, Any]]:
    """동기 함수 — 호출자가 asyncio.to_thread로 감싸서 이벤트 루프를 막지 않게 해야 한다.

    국내 6자리 종목코드 및 해외 티커(AAPL, VST 등)를 모두 지원한다.
    해외 티커는 로이터 코드(AAPL.O)로 자동 변환하여 네이버 한국어 뉴스를 조회하며,
    네이버 기사가 부족하거나 실패할 경우 yfinance로 자동 폴백한다.
    """
    clean_sym = symbol.strip().upper()
    is_overseas = symbol_mapper.is_overseas_symbol(clean_sym)
    query_code = symbol_mapper.ticker_to_reuters_code(clean_sym) if is_overseas else clean_sym

    items = []
    try:
        response = requests.get(
            API_URL.format(symbol=query_code),
            params={"pageSize": max_items, "page": 1},
            headers=_HEADERS,
            timeout=_TIMEOUT_SEC,
        )
        if response.status_code == 200 and response.text.strip():
            data = response.json()
            items = [item for group in (data or []) for item in (group.get("items") or [])]
    except Exception:
        logger.warning(f"'{symbol}' (code={query_code}) 네이버 뉴스 조회 실패", exc_info=True)

    articles = []
    for item in items[:max_items]:
        try:
            articles.append({
                "symbol": clean_sym,
                "source": "naver_finance_overseas" if is_overseas else "naver_finance",
                "url": item["mobileNewsUrl"],
                "title": html.unescape(item.get("titleFull") or item.get("title") or ""),
                "summary": html.unescape(item.get("body") or ""),
                "published_at": _parse_datetime(item["datetime"]),
            })
        except (KeyError, ValueError):
            continue

    # 해외 종목인데 네이버 뉴스가 없으면 yfinance 뉴스로 폴백
    if is_overseas and not articles:
        articles = yfinance_client.fetch_recent_news_yfinance(clean_sym, max_items=max_items)

    return articles

