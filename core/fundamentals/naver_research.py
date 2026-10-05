# core/fundamentals/naver_research.py
"""네이버페이 증권의 시장 전체 '종목분석' 증권사 리포트 목록 (비공식 엔드포인트, 주당 약 100건).
naver_consensus.py와 같은 원칙으로 실패 시 예외 대신 빈 리스트를 반환한다."""
import html
import logging
import re
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

API_URL = "https://m.stock.naver.com/api/research/company"
DETAIL_API_URL = "https://m.stock.naver.com/api/research/company/{research_id}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT_SEC = 10
_PAGE_SIZE = 100


def clean_report_html(raw_html: str) -> str:
    """리포트 본문 HTML을 순수 텍스트로 정리 (태그 제거, 개행 보존, 공백 축소)."""
    if not raw_html:
        return ""
    text = html.unescape(raw_html)
    text = re.sub(r"<(br|p|div|li)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


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
            write_date_raw = row.get("writeDate") or ""
            reports.append({
                "symbol": row["itemCode"],
                "name": row.get("itemName"),
                "broker": row.get("brokerName"),
                "title": row.get("title"),
                "date": write_date_raw.replace("-", ""),
                "write_date": write_date_raw,
                "read_count": int(row.get("readCount") or 0),
                "research_id": int(row.get("researchId") or 0),
                "end_url": row.get("endUrl") or f"https://m.stock.naver.com/research/company/{row.get('researchId')}",
            })
        if len(rows) < _PAGE_SIZE:
            break
    return reports


def fetch_research_detail(research_id: int) -> Dict[str, Any]:
    """동기 함수 — 리포트 상세(본문 텍스트, 투자의견, 목표가, PDF 링크 등)를 가져온다."""
    if not research_id:
        return {}
    try:
        response = requests.get(
            DETAIL_API_URL.format(research_id=research_id),
            headers=_HEADERS,
            timeout=_TIMEOUT_SEC,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.warning(f"네이버 증권사 리포트 상세({research_id}) 조회 실패", exc_info=True)
        return {}

    content_data = data.get("researchContent") or {}
    raw_content = content_data.get("content") or ""

    def _parse_float(val: Any) -> float | None:
        if not val or val == "없음":
            return None
        try:
            return float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            return None

    return {
        "research_id": research_id,
        "content_text": clean_report_html(raw_content),
        "opinion": content_data.get("opinion") if content_data.get("opinion") != "없음" else None,
        "target_price": _parse_float(content_data.get("prevGoalPrice")),
        "price_at_write": _parse_float(content_data.get("priceAtWriteDate")),
        "attach_url": content_data.get("attachUrl"),
    }

