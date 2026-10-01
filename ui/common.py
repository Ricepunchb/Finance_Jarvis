# ui/common.py
"""app.py(분석 대시보드)와 각 탭이 공유하는 API 호출/포맷터. 읽기 전용이라 api_get만 둔다.

pages/1_KIS_자동매매.py에도 같은 이름의 헬퍼가 있다 — 그쪽은 건드리지 않으므로 동작을 맞춰 둔 것이다
(실패 시 예외 대신 None, 호출부가 `or {}` 등으로 처리).
"""
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests
import streamlit as st

API_BASE = os.environ.get("JARVIS_API_BASE", "http://127.0.0.1:8800")
KST = timezone(timedelta(hours=9))

REASON_LABELS = {
    "STOP_LOSS": "🚨 손절",
    "TRAILING_TAKE_PROFIT": "💰 트레일링 익절",
    "swing_signal": "🌀 스윙 시그널",
    "band_ceiling_forced_trim": "📉 밴드상한 강제축소",
    "onboarding_tranche": "🧩 분할 온보딩",
}
STATUS_LABELS = {
    "FILLED": "체결", "PARTIALLY_FILLED": "부분체결", "REJECTED": "거부", "CANCELLED": "취소",
    "NOT_SUBMITTED": "미전송", "SUBMITTED": "전송됨(미체결)", "PENDING": "대기", "UNKNOWN": "미확인",
}
MARKER_LABELS = {"executed": "체결", "rejected": "거부·취소", "not_submitted": "미전송", "pending": "미체결", "noop": "관망"}


def api_get(path: str, timeout: float = 20, **params) -> Optional[Any]:
    try:
        resp = requests.get(f"{API_BASE}{path}", params=params or None, timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code >= 400:
        return None
    return resp.json()


def cached_get(path: str, ttl: float = 60, timeout: float = 180, fresh: bool = False, **params) -> Optional[Any]:
    """세션 단위 TTL 캐시. 실패(None)는 캐시하지 않아 다음 런에서 재시도한다.

    fresh=True면 캐시를 무시하고 서버에 refresh=true로 새로 받되, 결과는 같은 캐시 키에 저장한다.
    """
    store: Dict[str, Any] = st.session_state.setdefault("_api_cache", {})
    key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    hit = store.get(key)
    if not fresh and hit and time.time() - hit[0] < ttl:
        return hit[1]
    data = api_get(path, timeout=timeout, **({"refresh": "true"} if fresh else {}), **params)
    if data is not None:
        store[key] = (time.time(), data)
    return data


def clear_cache() -> None:
    st.session_state.pop("_api_cache", None)


def fmt_kst(ts: Optional[float], fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, tz=KST).strftime(fmt) if ts else "-"


def fmt_krw(v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+,.0f}원" if signed else f"{v:,.0f}원"


def fmt_pct(v: Optional[float], signed: bool = False, digits: int = 2) -> str:
    """v는 비율(0.0123 → 1.23%)."""
    if v is None:
        return "-"
    return f"{v * 100:+.{digits}f}%" if signed else f"{v * 100:.{digits}f}%"


def symbol_label(symbol: str, names: Optional[Dict[str, str]]) -> str:
    name = (names or {}).get(symbol)
    return f"{name}({symbol})" if name else symbol


def today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")
