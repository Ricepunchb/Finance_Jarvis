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
import asyncio
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

import aiosqlite
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DEFAULT_MAP_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "etf_intelligence_map.json"
_ETF_MAP_CACHE: Optional[Dict[str, Dict[str, Any]]] = None

# 비정상 티커/일반 단어 필터링 목록
_INVALID_TICKER_WORDS = {
    "SEMICONDUCTORS", "SEMICONDUCTOR", "TECHNOLOGY", "TECH", "CHIPS", "CHIP",
    "INDEX", "ETF", "NONE", "NULL", "FUNDS", "FUND", "ENERGY", "FINANCE",
    "EQUITY", "STOCK", "STOCKS", "OPTION", "INCOME", "LEVERAGED", "SHORT"
}


class _ETFClassificationRawSchema(BaseModel):
    is_etf: bool = Field(default=True, description="이 종목이 ETF(상장지수펀드)인지 여부. 일반 개별 주식이면 false")
    type: str = Field(
        default="BROAD_INDEX",
        description="SINGLE_STOCK (단일종목 커버드콜/인컴), THEMATIC (섹터/테마/레버리지), BROAD_INDEX (시장 대표지수/배당/채권) 중 하나"
    )
    underlying: Optional[str] = Field(
        default=None,
        description="SINGLE_STOCK인 경우에만 해당 원본 기업의 주식 티커 1개 (예: TSLA, NVDA, COIN). 실제 주식 티커여야 함."
    )
    anchors: List[str] = Field(
        default_factory=list,
        description="THEMATIC인 경우 주가에 핵심 영향을 미치는 상위 핵심 기업 주식 티커 1~3개 (예: SOXL -> ['NVDA', 'AVGO', 'AMD']). 실제 미국 주식 티커여야 함."
    )
    theme: Optional[str] = Field(default=None, description="테마/섹터의 짧은 한국어 설명 (예: '반도체 3배 레버리지', '비만치료제')")

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


def save_etf_map_item(symbol: str, profile: Dict[str, Any]) -> None:
    """새롭게 판정된 ETF 프로필을 etf_intelligence_map.json 파일 및 메모리 캐시에 영구 저장한다."""
    global _ETF_MAP_CACHE
    clean_sym = symbol.strip().upper()
    if _ETF_MAP_CACHE is None:
        load_etf_map()

    assert _ETF_MAP_CACHE is not None
    _ETF_MAP_CACHE[clean_sym] = profile
    try:
        with open(_DEFAULT_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(_ETF_MAP_CACHE, f, ensure_ascii=False, indent=2)
        logger.info(f"✨ [ETF 자동 분류 영구 저장] {clean_sym} ➔ {profile}")
    except Exception as e:
        logger.warning(f"ETF 매핑 파일 저장 실패 ({clean_sym}): {e}")


def _is_valid_ticker(val: Any) -> bool:
    """단어(Semiconductors, None 등)가 아닌 실제 유효 주식 티커 형식인지 검증."""
    if not val or not isinstance(val, str):
        return False
    cleaned = re.sub(r"[^A-Z0-9.-]", "", val.strip().upper())
    if not cleaned or len(cleaned) > 6 or len(cleaned) < 1:
        return False
    if cleaned in _INVALID_TICKER_WORDS:
        return False
    # 순수 숫자는 국내 주식코드(예: 005930), 알파벳은 미국 티커(예: TSLA)
    return True


def _clean_ticker(val: Any) -> Optional[str]:
    if not _is_valid_ticker(val):
        return None
    return re.sub(r"[^A-Z0-9.-]", "", str(val).strip().upper())


def _classify_via_holdings(clean_sym: str, t: Any, info: dict) -> Optional[Dict[str, Any]]:
    """yfinance funds_data.top_holdings를 통한 펀드 실제 편입 종목 기반 자동 분류."""
    try:
        th = t.funds_data.top_holdings
        if th is None or th.empty:
            return None

        valid_holdings = []
        for idx, row in th.iterrows():
            sym_raw = str(idx).strip().upper()
            pct = float(row.get("Holding Percent", 0.0) or 0.0)
            name = str(row.get("Name", ""))
            c_sym = _clean_ticker(sym_raw)
            if c_sym and c_sym != clean_sym:
                valid_holdings.append((c_sym, pct, name))

        if not valid_holdings:
            return None

        top_sym, top_pct, top_name = valid_holdings[0]

        # 1. 1위 비중 50% 이상 ➔ 단일 기초자산 ETF (SINGLE_STOCK)
        if top_pct >= 0.50:
            return {
                "type": "SINGLE_STOCK",
                "underlying": top_sym,
                "name": info.get("shortName") or info.get("longName") or clean_sym,
                "auto_discovered": True,
            }

        # 2. 1위 비중 8% 이상 ➔ 특정 섹터/테마 집중형 ETF (THEMATIC)
        if top_pct >= 0.08:
            anchors = [s for s, _, _ in valid_holdings[:3]]
            theme = info.get("category") or info.get("shortName") or "섹터/테마"
            return {
                "type": "THEMATIC",
                "anchors": anchors,
                "theme": theme,
                "name": info.get("shortName") or info.get("longName") or clean_sym,
                "auto_discovered": True,
            }

        # 3. 1위 비중이 낮고 고르게 분산됨 ➔ 광범위 시장 지수형 (BROAD_INDEX)
        return {
            "type": "BROAD_INDEX",
            "macro_driven": True,
            "name": info.get("shortName") or info.get("longName") or clean_sym,
            "auto_discovered": True,
        }
    except Exception as e:
        logger.debug(f"top_holdings 기반 자동 분류 생략 ({clean_sym}): {e}")
        return None


def _classify_via_gemini_safe(clean_sym: str, info: dict) -> Optional[Dict[str, Any]]:
    """Gemini LLM을 통한 ETF 성격 및 핵심 앵커 주식 자동 추론 (파생/스왑형 레버리지 ETF 대비)."""
    try:
        from core.llm.gemini_provider import GeminiProvider
        provider = GeminiProvider()
    except Exception as e:
        logger.warning(f"GeminiProvider 초기화 실패로 LLM 분류 건너뜀 ({clean_sym}): {e}")
        return None

    name = info.get("shortName") or info.get("longName") or clean_sym
    category = info.get("category") or ""
    summary = (info.get("longBusinessSummary") or "")[:500]

    prompt = f"""다음 미국/글로벌 ETF 종목의 정보를 분석하여 ETF 인텔리전스 분류를 수행하라.

종목 티커: {clean_sym}
종목명: {name}
카테고리: {category}
펀드 설명: {summary}

[지침]
1. type은 반드시 'SINGLE_STOCK', 'THEMATIC', 'BROAD_INDEX' 중 하나로 분류하라.
   - SINGLE_STOCK: 단일 기업 주식을 추종/옵션인컴(예: TSLW/TSLY -> TSLA, NVDY -> NVDA)
   - THEMATIC: 특정 산업/테마/레버리지 ETF(예: SOXL -> NVDA, AVGO, AMD / OZEM -> LLY, NVO)
   - BROAD_INDEX: S&P500, 나스닥100, 국채, 금 등 광범위 지수(예: VOO, QQQ, SGOV, GLD)
2. anchors는 반드시 단어가 아닌 실제 주식 티커 심볼(예: NVDA, AVGO, AMD 등 1~3개)로 작성하라. 단어나 업종명(Semiconductors, Tech 등)을 절대 넣지 마라.
3. SINGLE_STOCK인 경우 underlying에 원본 단일 주식 티커 1개를 기재하라.
"""

    try:
        raw: _ETFClassificationRawSchema = asyncio.run(
            provider._generate(prompt, _ETFClassificationRawSchema, temperature=0.1)
        )
    except Exception as e:
        logger.warning(f"Gemini ETF 분류 질의 실패 ({clean_sym}): {e}")
        return None

    if not raw or not raw.is_etf:
        return None

    # 방어적 타입 정규화
    raw_type = (raw.type or "").strip().upper()
    underlying = _clean_ticker(raw.underlying)
    if underlying == clean_sym:
        underlying = None

    anchors = []
    if raw.anchors:
        for a in raw.anchors:
            c_a = _clean_ticker(a)
            if c_a and c_a != clean_sym and c_a not in anchors:
                anchors.append(c_a)
    anchors = anchors[:3]

    if underlying or "SINGLE" in raw_type:
        etf_type = "SINGLE_STOCK"
        underlying = underlying or (anchors[0] if anchors else None)
    elif "THEME" in raw_type or "SECTOR" in raw_type or "LEVERAGE" in raw_type or len(anchors) >= 1:
        etf_type = "THEMATIC"
    elif "INDEX" in raw_type or "BROAD" in raw_type:
        etf_type = "BROAD_INDEX"
    else:
        etf_type = "THEMATIC" if anchors else "BROAD_INDEX"

    theme = raw.theme or category or "글로벌 테마"

    if etf_type == "SINGLE_STOCK" and underlying:
        return {
            "type": "SINGLE_STOCK",
            "underlying": underlying,
            "name": name,
            "auto_discovered": True,
        }
    elif etf_type == "THEMATIC" and anchors:
        return {
            "type": "THEMATIC",
            "anchors": anchors,
            "theme": theme,
            "name": name,
            "auto_discovered": True,
        }
    else:
        return {
            "type": "BROAD_INDEX",
            "macro_driven": True,
            "name": name,
            "auto_discovered": True,
        }


def resolve_etf_profile_sync(
    symbol: str, conn: Optional[aiosqlite.Connection] = None
) -> Optional[Dict[str, Any]]:
    """동기적으로 ETF 프로필을 해결(기존 맵 확인 ➔ 동적 펀드분석 ➔ Gemini 추론 ➔ 영구 캐싱)."""
    clean_sym = symbol.strip().upper()
    etf_map = load_etf_map()

    # 1. 기존 캐시 파일에 있으면 즉시 반환 (0.001초)
    if clean_sym in etf_map:
        return etf_map[clean_sym]

    # 2. 국내 stock_master DB 확인 (국내 ETF 접두어)
    # (동기 컨텍스트이므로 conn이 비동기 Connection이면 안전하게 스킵하거나 yfinance/Gemini로 폴백)

    # 3. yfinance를 통한 기본 메타데이터 확인
    import yfinance as yf
    try:
        t = yf.Ticker(clean_sym)
        info = t.info or {}
    except Exception as e:
        logger.warning(f"Ticker 메타데이터 조회 실패 ({clean_sym}): {e}")
        return None

    quote_type = (info.get("quoteType") or "").upper()
    if quote_type == "EQUITY":
        # 명백한 개별 기업 주식 (예: AAPL, TSLA, NFLX 등)
        return None

    short_name = (info.get("shortName") or "").upper()
    long_name = (info.get("longName") or "").upper()

    is_fund_or_etf = (
        quote_type in ("ETF", "MUTUALFUND")
        or bool(info.get("category"))
        or bool(re.search(r"\bETF\b", short_name))
        or bool(re.search(r"\bETF\b", long_name))
    )

    if not is_fund_or_etf:
        # 일반 개별 주식으로 확인됨
        return None

    # 4. 펀드 실제 편입 종목(top_holdings)으로 1차 시도
    profile = _classify_via_holdings(clean_sym, t, info)
    if profile:
        save_etf_map_item(clean_sym, profile)
        return profile

    # 5. 스왑/레버리지 파생형 ETF에 대한 Gemini 자동 분류 시도
    profile = _classify_via_gemini_safe(clean_sym, info)
    if profile:
        save_etf_map_item(clean_sym, profile)
        return profile

    # 6. 최후 폴백: 기본 광범위 지수형으로 안전하게 등록
    fallback_profile = {
        "type": "BROAD_INDEX",
        "macro_driven": True,
        "name": info.get("shortName") or info.get("longName") or clean_sym,
        "auto_discovered": True,
    }
    save_etf_map_item(clean_sym, fallback_profile)
    return fallback_profile


async def resolve_etf_profile(
    conn: Optional[aiosqlite.Connection], symbol: str
) -> Optional[Dict[str, Any]]:
    """비동기 ETF 인텔리전스 프로필 해결자."""
    clean_sym = symbol.strip().upper()
    etf_map = load_etf_map()
    if clean_sym in etf_map:
        return etf_map[clean_sym]

    # 국내 ETF DB 확인
    if conn is not None:
        try:
            cur = await conn.execute("SELECT name FROM stock_master WHERE symbol = ?", (clean_sym,))
            row = await cur.fetchone()
            if row and row[0]:
                name = row[0].strip()
                upper_name = name.upper()
                is_etf = any(upper_name.startswith(p) for p in _KOREAN_ETF_PREFIXES) or "ETF" in upper_name
                if is_etf:
                    for kw, underlying in _DYNAMIC_SINGLE_STOCK_KEYWORDS.items():
                        if kw in upper_name:
                            p = {"type": "SINGLE_STOCK", "underlying": underlying, "name": name, "dynamic": True}
                            save_etf_map_item(clean_sym, p)
                            return p
                    p = {"type": "BROAD_INDEX", "macro_driven": True, "name": name, "dynamic": True}
                    save_etf_map_item(clean_sym, p)
                    return p
        except Exception:
            pass

    return await asyncio.to_thread(resolve_etf_profile_sync, clean_sym, conn)


async def get_etf_profile(
    conn: Optional[aiosqlite.Connection], symbol: str
) -> Optional[Dict[str, Any]]:
    """하위 호환용 래퍼: resolve_etf_profile을 호출한다."""
    return await resolve_etf_profile(conn, symbol)

