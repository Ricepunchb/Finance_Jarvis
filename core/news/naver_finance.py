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

    실패해도 예외를 올리지 않고 빈 리스트를 반환한다 (뉴스가 없다고 해서 매매를
    막을 이유는 없고, 단지 sentiment_signal이 없는 것으로 처리되면 된다).
    """
    try:
        response = requests.get(
            API_URL.format(symbol=symbol),
            params={"pageSize": max_items, "page": 1},
            headers=_HEADERS,
            timeout=_TIMEOUT_SEC,
        )
        response.raise_for_status()
        data = response.json()
        items = data[0].get("items", []) if data else []
    except Exception:
        logger.warning(f"'{symbol}' 뉴스 조회 실패 - 이번 사이클은 뉴스 없이 진행", exc_info=True)
        return []

    articles = []
    for item in items[:max_items]:
        try:
            articles.append({
                "symbol": symbol,
                "source": "naver_finance",
                "url": item["mobileNewsUrl"],
                "title": html.unescape(item.get("titleFull") or item.get("title") or ""),
                "summary": html.unescape(item.get("body") or ""),
                "published_at": _parse_datetime(item["datetime"]),
            })
        except (KeyError, ValueError):
            continue  # 형식이 안 맞는 항목 하나는 건너뛰고 나머지는 계속 사용
    return articles
