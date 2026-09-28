# core/fundamentals/naver_research.py
"""네이버페이 증권의 시장 전체 '종목분석' 증권사 리포트 목록 (비공식 엔드포인트, 주당 약 100건).
naver_consensus.py와 같은 원칙으로 실패 시 예외 대신 빈 리스트를 반환한다."""
import logging
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

API_URL = "https://m.stock.naver.com/api/research/company"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT_SEC = 10
_PAGE_SIZE = 100


def fetch_company_research(pages: int = 2) -> List[Dict[str, Any]]:
    """동기 함수 — 호출자가 asyncio.to_thread로 감싸야 한다. 최신순."""
    reports: List[Dict[str, Any]] = []
    for page in range(1, pages + 1):
        try:
            response = requests.get(
                API_URL, params={"pageSize": _PAGE_SIZE, "page": page}, headers=_HEADERS, timeout=_TIMEOUT_SEC,
            )
            response.raise_for_status()
            rows = response.json()
        except Exception:
            logger.warning(f"네이버 증권사 리포트 목록 {page}페이지 조회 실패", exc_info=True)
            break
        for row in rows:
            if not row.get("itemCode"):
                continue
            reports.append({
                "symbol": row["itemCode"],
                "name": row.get("itemName"),
                "broker": row.get("brokerName"),
                "title": row.get("title"),
                "date": (row.get("writeDate") or "").replace("-", ""),
                "read_count": int(row.get("readCount") or 0),
            })
        if len(rows) < _PAGE_SIZE:
            break
    return reports
