# core/news/etf_mapper.py
"""3계층 ETF 인텔리전스 매핑 모듈.

ETF를 성격별로 3가지로 분류하여 뉴스 및 시그널 상속 구조를 결정한다:
1. SINGLE_STOCK: 단일 기초자산 ETF (TSLW, NFLW, 494340 ACE테슬라밸류체인 등)
   -> 기초자산 주식(TSLA, NFLX 등)의 뉴스 감성을 100% 프록시 상속.
2. THEMATIC: 특정 테마/섹터 ETF (OZEM, SMH, SOXX, 305720 2차전지 등)
   -> 상위 앵커 기업(OZEM은 LLY+NVO, SMH는 NVDA+TSM 등)의 감성 가중평균 상속.
3. BROAD_INDEX: 광범위 지수/배당형 ETF (SCHD, SPY, QQQ, 446720 SOL미배당 등)
   -> 개별 종목 뉴스를 생략하고 매크로 컨텍스트(Fear&Greed, 금리) 및 기술적 퀀트 지표 주력.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_DEFAULT_MAP_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "etf_intelligence_map.json"
_ETF_MAP_CACHE: Optional[Dict[str, Dict[str, Any]]] = None

# 국내 ETF 브랜드 접두어
_KOREAN_ETF_PREFIXES = (
    "KODEX", "TIGER", "ACE", "SOL", "RISE", "KBSTAR", "PLUS", "WOORI", "KOSEF",
    "HANARO", "TIMEFOLIO", "HERO", "UNICORN", "TRUSTON", "마이티", "파워"
)

# 대표적인 단일 종목 키워드 매핑 (동적 해석용)
_DYNAMIC_SINGLE_STOCK_KEYWORDS = {
    "테슬라": "TSLA",
    "TESLA": "TSLA",
    "엔비디아": "NVDA",
    "NVIDIA": "NVDA",
    "애플": "AAPL",
    "APPLE": "AAPL",
    "마이크로소프트": "MSFT",
    "구글": "GOOGL",
    "알파벳": "GOOGL",
    "아마존": "AMZN",
    "팔란티어": "PLTR",
    "삼성전자": "005930",
    "SK하이닉스": "000660",
    "현대차": "005380",
}


def load_etf_map(force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    """etf_intelligence_map.json 파일에서 ETF 매핑 정보를 로드한다."""
    global _ETF_MAP_CACHE
    if _ETF_MAP_CACHE is not None and not force_reload:
        return _ETF_MAP_CACHE

    if not _DEFAULT_MAP_PATH.exists():
        logger.warning(f"ETF 매핑 파일이 존재하지 않습니다: {_DEFAULT_MAP_PATH}")
        _ETF_MAP_CACHE = {}
        return _ETF_MAP_CACHE

    try:
        with open(_DEFAULT_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            _ETF_MAP_CACHE = {k.strip().upper(): v for k, v in data.items()}
            return _ETF_MAP_CACHE
    except Exception as e:
        logger.exception(f"ETF 매핑 파일 로드 실패: {e}")
        _ETF_MAP_CACHE = {}
        return _ETF_MAP_CACHE


async def get_etf_profile(
    conn: Optional[aiosqlite.Connection], symbol: str
) -> Optional[Dict[str, Any]]:
    """종목코드의 ETF 인텔리전스 프로필을 반환한다.
    
    ETF가 아닌 일반 개별 종목인 경우 None을 반환한다.
    
    Returns:
        Optional[Dict[str, Any]]: {
            "type": "SINGLE_STOCK" | "THEMATIC" | "BROAD_INDEX",
            "underlying": "TSLA" (SINGLE_STOCK인 경우),
            "anchors": ["LLY", "NVO"] (THEMATIC인 경우),
            "theme": "비만치료제" (THEMATIC인 경우),
            "macro_driven": True (BROAD_INDEX인 경우),
            "name": "..."
        } 또는 None
    """
    clean_sym = symbol.strip().upper()
    etf_map = load_etf_map()

    # 1. 정적 매핑 파일에 등록되어 있는 경우
    if clean_sym in etf_map:
        return etf_map[clean_sym]

    # 2. stock_master DB에서 ETF 여부 및 명칭 동적 확인
    if conn is not None:
        try:
            cur = await conn.execute("SELECT name FROM stock_master WHERE symbol = ?", (clean_sym,))
            row = await cur.fetchone()
            if row and row[0]:
                name = row[0].strip()
                upper_name = name.upper()

                # ETF 브랜드 접두어로 시작하거나 명칭에 'ETF'가 들어가는 경우
                is_etf = any(upper_name.startswith(p) for p in _KOREAN_ETF_PREFIXES) or "ETF" in upper_name
                if is_etf:
                    # 단일 종목 키워드가 포함되어 있는지 동적 검사 (예: ACE 테슬라... -> TSLA)
                    for kw, underlying in _DYNAMIC_SINGLE_STOCK_KEYWORDS.items():
                        if kw in upper_name:
                            return {
                                "type": "SINGLE_STOCK",
                                "underlying": underlying,
                                "name": name,
                                "dynamic": True,
                            }
                    # 단일 종목이 아닌 일반 ETF는 기본 광범위/지수형으로 안전하게 처리
                    return {
                        "type": "BROAD_INDEX",
                        "macro_driven": True,
                        "name": name,
                        "dynamic": True,
                    }
        except Exception:
            pass

    return None

