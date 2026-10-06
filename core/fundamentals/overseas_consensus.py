# core/fundamentals/overseas_consensus.py
"""네이버페이 증권 해외주식 API 및 yfinance 기반 애널리스트 컨센서스 & 리서치 수집 모듈.

국내 종목의 core/fundamentals/naver_consensus.py와 동일한 인터페이스를 제공하여
밸류에이션(목표가 괴리율), PER, PBR, 52주 고저 및 증권사 리서치 리포트를 제공한다.
네이버 엔드포인트가 비어있거나 실패하면 yfinance로 자동 폴백한다.
실패 시 예외 대신 빈 dict/list를 반환한다.
"""
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from core.fundamentals import yfinance_client
from core.utils import symbol_mapper

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 5
_HEADERS = {"User-Agent": "Mozilla/5.0"}

_BASIC_API_URL = "https://api.stock.naver.com/stock/{reuters_code}/basic"
_CONSENSUS_API_URL = "https://api.stock.naver.com/stock/{reuters_code}/consensus"
_RESEARCH_API_URL = "https://api.stock.naver.com/stock/{reuters_code}/research"


def _to_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(raw))
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_overseas_consensus(ticker: str, exchange: Optional[str] = None) -> Dict[str, Any]:
    """해외 종목 애널리스트 컨센서스(목표주가, 투자의견, PER, PBR 등)를 가져온다 (동기 함수)."""
    clean_ticker = ticker.strip().upper()
    reuters_code = symbol_mapper.ticker_to_reuters_code(clean_ticker, exchange)

    consensus_data: Dict[str, Any] = {}
    basic_data: Dict[str, Any] = {}

    # 1. 네이버 컨센서스 조회
    try:
        resp = requests.get(_CONSENSUS_API_URL.format(reuters_code=reuters_code), headers=_HEADERS, timeout=_TIMEOUT_SEC)
        if resp.status_code == 200 and resp.text.strip():
            consensus_data = resp.json() or {}
    except Exception:
        logger.debug(f"네이버 해외 컨센서스 조회 실패 ({reuters_code})", exc_info=True)

    # 2. 네이버 기본 시세 조회 (PER/PBR/52주 고저 등)
    try:
        resp = requests.get(_BASIC_API_URL.format(reuters_code=reuters_code), headers=_HEADERS, timeout=_TIMEOUT_SEC)
        if resp.status_code == 200 and resp.text.strip():
            basic_data = resp.json() or {}
    except Exception:
        logger.debug(f"네이버 해외 기본시세 조회 실패 ({reuters_code})", exc_info=True)

    total_infos = {}
    for item in (basic_data.get("stockItemTotalInfos") or []):
        if isinstance(item, dict) and "code" in item:
            total_infos[item["code"]] = item.get("value")

    target_mean = _to_float(consensus_data.get("priceTargetMean"))
    recomm_mean = _to_float(consensus_data.get("recommMean"))
    last_close = _to_float(basic_data.get("closePrice")) or _to_float(basic_data.get("closePriceRaw"))
    w52_high = _to_float(total_infos.get("highPriceOf52Weeks"))
    w52_low = _to_float(total_infos.get("lowPriceOf52Weeks"))
    per = _to_float(total_infos.get("per"))
    pbr = _to_float(total_infos.get("pbr"))

    # 3. 네이버 컨센서스 데이터가 부족하면 yfinance로 폴백 보강
    if target_mean is None or last_close is None:
        yf_data = yfinance_client.fetch_analyst_targets(clean_ticker)
        if yf_data:
            if target_mean is None:
                target_mean = yf_data.get("target_price_mean")
            if recomm_mean is None:
                recomm_mean = yf_data.get("recomm_mean")
            if last_close is None:
                last_close = yf_data.get("last_close")
            if w52_high is None:
                w52_high = yf_data.get("w52_high")
            if w52_low is None:
                w52_low = yf_data.get("w52_low")
            if per is None:
                per = yf_data.get("per")
            if pbr is None:
                pbr = yf_data.get("pbr")

    researches = fetch_overseas_research(clean_ticker, exchange)

    return {
        "symbol": clean_ticker,
        "target_price_mean": target_mean,
        "recomm_mean": recomm_mean,
        "per": per,
        "pbr": pbr,
        "w52_high": w52_high,
        "w52_low": w52_low,
        "last_close": last_close,
        "researches": researches,
    }


def fetch_overseas_research(ticker: str, exchange: Optional[str] = None) -> List[Dict[str, Any]]:
    """해외 종목 리포트 목록(네이버 모닝스타 리포트 및 yfinance 등급변동 이력)을 수집한다."""
    clean_ticker = ticker.strip().upper()
    reuters_code = symbol_mapper.ticker_to_reuters_code(clean_ticker, exchange)

    reports: List[Dict[str, Any]] = []

    # 1. 네이버 모닝스타 리포트 조회
    try:
        resp = requests.get(_RESEARCH_API_URL.format(reuters_code=reuters_code), headers=_HEADERS, timeout=_TIMEOUT_SEC)
        if resp.status_code == 200 and resp.text.strip():
            raw_reports = resp.json() or []
            for r in raw_reports:
                if not isinstance(r, dict):
                    continue
                reports.append({
                    "symbol": clean_ticker,
                    "broker": "Morningstar",
                    "title": r.get("title") or "Morningstar Research",
                    "date": (r.get("analystNotePublishDate") or r.get("contentThesisPublishDate") or "").replace(".", ""),
                    "fair_value": _to_float(r.get("fairValue")),
                    "rating": r.get("rating"),
                    "economic_moat": (r.get("economicMoatType") or {}).get("name") if isinstance(r.get("economicMoatType"), dict) else None,
                    "attach_url": r.get("originalPDF"),
                    "read_count": 0,
                    "research_id": r.get("researchId"),
                })
    except Exception:
        logger.debug(f"네이버 해외 리서치 조회 실패 ({reuters_code})", exc_info=True)

    # 2. 모닝스타 리포트가 없으면 yfinance upgrades/downgrades 이력으로 보강
    if not reports:
        yf_upgrades = yfinance_client.fetch_upgrades_downgrades(clean_ticker, max_items=5)
        for u in yf_upgrades:
            reports.append({
                "symbol": clean_ticker,
                "broker": u.get("broker", "WallStreet"),
                "title": u.get("title", ""),
                "date": u.get("date", ""),
                "fair_value": None,
                "rating": None,
                "economic_moat": None,
                "attach_url": None,
                "read_count": 0,
                "research_id": None,
            })

    return reports

