# core/utils/symbol_mapper.py
"""미국 주식 티커 ↔ 네이버 로이터 코드(reutersCode) ↔ 거래소 상호 변환 유틸리티.

네이버 증권은 나스닥 종목에 '.O' 접미사를 붙이고(예: AAPL.O, TSLA.O),
NYSE 및 AMEX 종목은 접미사 없이 티커 그대로 사용한다(예: VST, GLD, LLY).
이 모듈은 사전 정의된 정적 매핑과 네이버 자동완성 API(ac.stock.naver.com)를 통한
동적 조회를 결합하고 메모리 캐싱을 수행한다.
"""
import logging
import re
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 5
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_NAVER_AC_URL = "https://ac.stock.naver.com/ac"

# 자주 쓰이는 주요 종목 정적 매핑 (API 호출 절약 및 오프라인 안전장치)
_STATIC_REUTERS_MAP: Dict[str, str] = {
    # 빅테크 & 지수
    "AAPL": "AAPL.O",
    "MSFT": "MSFT.O",
    "NVDA": "NVDA.O",
    "TSLA": "TSLA.O",
    "GOOGL": "GOOGL.O",
    "AMZN": "AMZN.O",
    "META": "META.O",
    "QQQ": "QQQ.O",
    "SPY": "SPY",
    "GLD": "GLD",
    # AI 전력 인프라
    "VST": "VST",
    "CEG": "CEG.O",
    "GEV": "GEV",
    "ETN": "ETN",
    # 헬스케어 / 비만치료제
    "LLY": "LLY",
    "NVO": "NVO",
    # 경기방어주 / 배당주
    "KO": "KO",
    "PG": "PG",
    "JNJ": "JNJ",
    "NEE": "NEE",
    "XLU": "XLU",
    "IBM": "IBM",
}

# 런타임 캐시 (ticker.upper() -> reuters_code)
_RUNTIME_CACHE: Dict[str, str] = dict(_STATIC_REUTERS_MAP)
_INFO_CACHE: Dict[str, Dict[str, Any]] = {}


def is_overseas_symbol(symbol: str) -> bool:
    """국내 6자리 숫자 종목코드(예: 005930)가 아닌 영문 티커인지 판정."""
    if not symbol:
        return False
    s = symbol.strip()
    return not (len(s) == 6 and s.isdigit())


def ticker_to_reuters_code(ticker: str, exchange: Optional[str] = None) -> str:
    """미국 티커(예: AAPL, VST)를 네이버 증권 API가 인식하는 로이터 코드로 변환한다.

    1. 캐시/정적 매핑 확인
    2. exchange 힌트 확인 (NASD인 경우 기본 '.O')
    3. 네이버 자동완성 API 조회
    4. 조회 실패 시 규칙 기반 폴백 (NASD는 .O, 그 외는 ticker 그대로)
    """
    clean_ticker = ticker.strip().upper()
    if clean_ticker in _RUNTIME_CACHE:
        return _RUNTIME_CACHE[clean_ticker]

    # 이미 .O 등이 붙어있는 경우 그대로 인정
    if "." in clean_ticker:
        return clean_ticker

    # 네이버 자동완성 API 동적 조회 시도
    info = search_naver_symbol(clean_ticker)
    if info and info.get("reutersCode"):
        code = info["reutersCode"]
        _RUNTIME_CACHE[clean_ticker] = code
        return code

    # 폴백 규칙: 거래소 힌트 활용
    if exchange and exchange.upper() in ("NASD", "NASDAQ", "NAS"):
        code = f"{clean_ticker}.O"
    else:
        code = clean_ticker

    _RUNTIME_CACHE[clean_ticker] = code
    return code


def reuters_code_to_ticker(reuters_code: str) -> str:
    """'AAPL.O' -> 'AAPL', 'VST' -> 'VST'."""
    clean = reuters_code.strip().upper()
    return clean.split(".")[0]


def search_naver_symbol(query: str) -> Optional[Dict[str, Any]]:
    """네이버 증권 자동완성 API를 조회하여 종목 메타데이터를 가져온다."""
    clean = query.strip().upper()
    if clean in _INFO_CACHE:
        return _INFO_CACHE[clean]

    try:
        resp = requests.get(
            _NAVER_AC_URL,
            params={"q": clean, "target": "stock"},
            headers=_HEADERS,
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("items") or []
        for item in items:
            if item.get("code") == clean or item.get("reutersCode") == clean:
                _INFO_CACHE[clean] = item
                if item.get("reutersCode"):
                    _RUNTIME_CACHE[clean] = item["reutersCode"]
                return item
        if items:
            # 완전 일치 항목이 없더라도 첫 번째 해외 주식 항목 반환
            first = items[0]
            _INFO_CACHE[clean] = first
            return first
    except Exception:
        logger.debug(f"네이버 심볼 검색 실패: {clean}", exc_info=True)
    return None

