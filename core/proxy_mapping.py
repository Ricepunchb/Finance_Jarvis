# core/proxy_mapping.py
"""해외 발굴 종목 ↔ 국내 상장 대체 ETF (Proxy ETF) 매핑 및 해석 모듈.

해외 종목(GOOGL, NVDA, GLD 등)의 글로벌 뉴스·월가 컨센서스·기술 지표로 투자 인사이트를
발굴하되, 해외 거래 수수료 및 환전 비용을 절감하기 위해 실제 주문·포트폴리오 편입을
국내 상장 대체 ETF(ACE 구글밸류체인액티브, KODEX 골드선물(H) 등)로 대리 매매할 때 사용된다.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAP_PATH = Path(__file__).resolve().parent.parent / "data" / "overseas_proxy_map.json"
_MAPPING_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def load_proxy_mappings(force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    """overseas_proxy_map.json 파일에서 해외 ↔ 국내 대체 ETF 매핑을 로드한다."""
    global _MAPPING_CACHE
    if _MAPPING_CACHE is not None and not force_reload:
        return _MAPPING_CACHE

    if not _DEFAULT_MAP_PATH.exists():
        logger.warning(f"프록시 매핑 파일이 존재하지 않습니다: {_DEFAULT_MAP_PATH}")
        _MAPPING_CACHE = {}
        return _MAPPING_CACHE

    try:
        with open(_DEFAULT_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 심볼 키는 대문자로 정규화
            _MAPPING_CACHE = {k.upper(): v for k, v in data.items()}
            return _MAPPING_CACHE
    except Exception as e:
        logger.exception(f"프록시 매핑 파일 로드 실패: {e}")
        _MAPPING_CACHE = {}
        return _MAPPING_CACHE


def get_proxy_etf(symbol: str) -> Optional[Dict[str, Any]]:
    """해외 티커(예: 'GOOGL', 'NVDA', 'GLD')에 대응하는 국내 대체 ETF 정보를 반환한다.
    
    Returns:
        Optional[Dict[str, Any]]: {
            "proxy_symbol": "473460",
            "proxy_name": "ACE 구글밸류체인액티브",
            "type": "VALUE_CHAIN",
            "beta_estimate": 0.85,
            "description": "..."
        } 또는 None
    """
    mappings = load_proxy_mappings()
    return mappings.get(symbol.strip().upper())


def get_reverse_proxy_targets(proxy_symbol: str) -> List[str]:
    """국내 ETF 종목코드(예: '473460', '483320')가 대리하는 해외 티커 목록을 반환한다."""
    mappings = load_proxy_mappings()
    targets = []
    clean_sym = proxy_symbol.strip()
    for us_ticker, info in mappings.items():
        if info.get("proxy_symbol") == clean_sym:
            targets.append(us_ticker)
    return targets


def resolve_order_symbol(symbol: str, proxy_enabled: bool = True) -> tuple[str, bool, Optional[Dict[str, Any]]]:
    """주문 집행 시 사용할 실제 종목코드와 프록시 적용 여부를 해석한다.
    
    Args:
        symbol: 원본 종목코드 (해외 티커 또는 국내 종목코드)
        proxy_enabled: 프록시 모드 활성화 여부
        
    Returns:
        (actual_symbol, is_proxied, proxy_info)
        예: ("473460", True, {...}) 또는 ("GOOGL", False, None)
    """
    if not proxy_enabled:
        return symbol, False, None

    proxy_info = get_proxy_etf(symbol)
    if proxy_info and "proxy_symbol" in proxy_info:
        return proxy_info["proxy_symbol"], True, proxy_info

    return symbol, False, None

