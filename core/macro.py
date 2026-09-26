# core/macro.py
"""시장 전체 심리 지표(CNN Fear & Greed Index) 조회 - AI 리밸런싱 LLM의 참고 맥락(macro_context)용.

CNN은 공식 공개 API가 없어 프론트엔드가 쓰는 비공식 JSON 엔드포인트를 그대로 호출한다
(개인 비상업적 용도 범위 - CNN 이용약관). UA/Referer/Origin 헤더 없이 호출하면 봇으로
간주돼 418을 반환하므로 브라우저 요청과 유사하게 맞춘다. 스키마가 예고 없이 바뀌거나
차단이 강화될 수 있는 외부 의존성이므로, 실패는 조용히 None을 반환하고 리밸런싱 자체는
이 맥락 없이도(기존처럼) 정상 동작해야 한다 - 매매 판단에는 전혀 관여하지 않는다.
"""
import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp
import aiosqlite

from core import db
from core.config import settings

logger = logging.getLogger(__name__)

_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Origin": "https://edition.cnn.com",
}

_CACHE_KEY_VALUE = "fear_greed_cache_json"
_CACHE_KEY_FETCHED_AT = "fear_greed_cached_at"

# CNN이 자체적으로 쓰는 점수 구간 (previous_close/week/month/year는 숫자만 오고 등급이
# 안 붙어있어서 현재 rating과 같은 기준으로 우리가 직접 매겨야 한다).
_RATING_BOUNDARIES = [(25, "extreme fear"), (45, "fear"), (55, "neutral"), (75, "greed")]
_RATING_KO = {
    "extreme fear": "극단적 공포", "fear": "공포", "neutral": "중립",
    "greed": "탐욕", "extreme greed": "극단적 탐욕",
}


def _rating_from_score(score: float) -> str:
    for threshold, rating in _RATING_BOUNDARIES:
        if score < threshold:
            return rating
    return "extreme greed"


async def _fetch_live() -> Optional[Dict[str, Any]]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _URL, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"CNN Fear&Greed 조회 실패 (status={resp.status})")
                    return None
                data = await resp.json(content_type=None)
                return data.get("fear_and_greed")
    except Exception:
        logger.exception("CNN Fear&Greed 조회 중 예외 - 이번엔 맥락 없이 진행")
        return None


def _format_context(fg: Dict[str, Any]) -> str:
    score = float(fg.get("score", 0))
    rating = fg.get("rating") or _rating_from_score(score)

    def label(key: str) -> str:
        v = fg.get(key)
        if v is None:
            return "-"
        v = float(v)
        return f"{v:.0f}({_RATING_KO.get(_rating_from_score(v), '-')})"

    return (
        f"CNN Fear & Greed Index: {score:.0f}점 ({_RATING_KO.get(rating, rating)}). "
        f"전일 {label('previous_close')}, 1주전 {label('previous_1_week')}, "
        f"1개월전 {label('previous_1_month')}, 1년전 {label('previous_1_year')}."
    )


async def get_fear_greed_context(conn: aiosqlite.Connection) -> Optional[str]:
    """캐시(TTL settings.FEAR_GREED_CACHE_TTL_HOURS)가 살아있으면 그대로 쓰고, 만료됐으면
    갱신을 시도한다. 갱신에 실패해도 만료된 캐시라도 있으면 없는 것보단 낫다고 보고 반환한다."""
    cached_at = await db.get_state(conn, _CACHE_KEY_FETCHED_AT)
    cached_value = await db.get_state(conn, _CACHE_KEY_VALUE)
    if cached_at and cached_value:
        if (time.time() - float(cached_at)) < settings.FEAR_GREED_CACHE_TTL_HOURS * 3600:
            return _format_context(json.loads(cached_value))

    fg = await _fetch_live()
    if fg is None:
        return _format_context(json.loads(cached_value)) if cached_value else None

    await db.set_state(conn, _CACHE_KEY_VALUE, json.dumps(fg))
    await db.set_state(conn, _CACHE_KEY_FETCHED_AT, str(time.time()))
    return _format_context(fg)
