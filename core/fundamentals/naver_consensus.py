# core/fundamentals/naver_consensus.py
"""네이버페이 증권(m.stock.naver.com)의 종목 통합조회 API에서 애널리스트 컨센서스
(목표주가)와 PER/PBR/52주 고저를 가져온다. core/news/naver_finance.py와 동일하게
비공식(문서화 안 된) 엔드포인트라 언제든 바뀔 수 있음을 전제로, 실패 시 예외를
올리지 않고 빈 dict를 반환한다 (밸류에이션 시그널이 없다고 매매를 막을 이유는 없다).
"""
import logging
import re
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

API_URL = "https://m.stock.naver.com/api/stock/{symbol}/integration"
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT_SEC = 10


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", raw)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_consensus(symbol: str) -> Dict[str, Any]:
    """동기 함수 — 호출자가 asyncio.to_thread로 감싸야 한다. 실패하면 빈 dict."""
    try:
        response = requests.get(API_URL.format(symbol=symbol), headers=_HEADERS, timeout=_TIMEOUT_SEC)
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.warning(f"'{symbol}' 네이버 컨센서스 조회 실패", exc_info=True)
        return {}

    consensus = data.get("consensusInfo") or {}
    total_infos = {item["code"]: item.get("value") for item in (data.get("totalInfos") or []) if "code" in item}

    return {
        "target_price_mean": _to_float(consensus.get("priceTargetMean")),
        "recomm_mean": _to_float(consensus.get("recommMean")),
        "per": _to_float(total_infos.get("per")),
        "pbr": _to_float(total_infos.get("pbr")),
        "w52_high": _to_float(total_infos.get("highPriceOf52Weeks")),
        "w52_low": _to_float(total_infos.get("lowPriceOf52Weeks")),
        "last_close": _to_float(total_infos.get("lastClosePrice")),
    }
