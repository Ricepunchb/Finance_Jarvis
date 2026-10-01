# core/master_files.py
"""KIS 정적 마스터 파일(전종목/테마) 다운로드·파싱. KIS REST에는 전종목 목록 TR이 없어서
종목 발굴의 모집단은 이 파일에서만 얻는다. 보통주와 ETF(레버리지·인버스·파생 기반 제외)를 후보로 허용하고
ETN·SPAC·관리/거래정지 등은 제외한다. 필드 위치는 Hantu-api/open-trading-api/stocks_info의
공식 파서(kis_kospi_code_mst.py 등)와 같고, 실제 파일로 검증했다.

행 구조: [단축코드 9][표준코드 12][한글명 가변] + 고정폭 꼬리(KOSPI 227자, KOSDAQ 221자) + 개행.
꼬리는 전부 ASCII라 디코딩된 문자열을 뒤에서부터 잘라도 안전하다.
"""
import asyncio
import io
import logging
import zipfile
from typing import Any, Dict, List, Optional

import aiohttp

from core import db
from core.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://new.real.download.dws.co.kr/common/master/"
_TIMEOUT = aiohttp.ClientTimeout(total=60)

# 꼬리 내 (시작, 끝) 오프셋
_LAYOUTS = {
    "KOSPI": {
        "file": "kospi_code.mst", "tail": 227,
        "group": (0, 2), "sector": (3, 7), "etp": (22, 23), "spac": (29, 30),
        "suspended": (60, 61), "liquidation": (61, 62), "managed": (62, 63), "warning": (63, 65),
        "preferred": (158, 159), "roe": (195, 204), "market_cap": (212, 221),
    },
    "KOSDAQ": {
        "file": "kosdaq_code.mst", "tail": 221,
        "group": (0, 2), "sector": (3, 7), "etp": (18, 19), "spac": (24, 25), "caution": (30, 31),
        "suspended": (55, 56), "liquidation": (56, 57), "managed": (57, 58), "warning": (58, 60),
        "preferred": (153, 154), "roe": (189, 198), "market_cap": (206, 215),
    },
}


def _to_float(raw: str) -> Optional[float]:
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _has_excluded_etf_keyword(name: str) -> bool:
    upper = name.upper()
    return any(k.strip().upper() in upper for k in settings.ETF_EXCLUDE_NAME_KEYWORDS.split(",") if k.strip())


def parse_stock_master_line(line: str, market: str) -> Optional[Dict[str, Any]]:
    layout = _LAYOUTS[market]
    line = line.rstrip("\r\n")
    tail_len = layout["tail"]
    if len(line) <= 21 + tail_len:
        return None
    head, tail = line[:-tail_len], line[-tail_len:]
    symbol = head[0:9].strip()
    name = head[21:].strip()
    if len(symbol) != 6 or not name:
        return None

    def field(key: str) -> str:
        start, end = layout[key]
        return tail[start:end]

    group, etp = field("group"), field("etp").strip()
    is_stock = group == "ST" and etp in ("", "0")
    # ETF: 그룹 EF + ETP 1(투자회사형)/2(수익증권형). EF라도 ETP 8(단일종목 레버리지)은 제외하고, ETN(그룹 EN)도 제외한다.
    is_etf = group == "EF" and etp in ("1", "2") and not _has_excluded_etf_keyword(name)
    is_excluded = (
        not (is_stock or is_etf)
        or field("spac") == "Y"
        or field("suspended") == "Y"
        or field("liquidation") == "Y"
        or field("managed") == "Y"
        or field("warning") != "00"
        or field("preferred") != "0"
        or ("caution" in layout and field("caution") == "Y")
    )
    return {
        "symbol": symbol,
        "name": name,
        "market": market,
        "sector_code": field("sector").strip() or None,
        "market_cap_eok": _to_float(field("market_cap")),
        "roe": _to_float(field("roe")),
        "is_excluded": is_excluded,
    }


def parse_theme_line(line: str) -> Optional[Dict[str, str]]:
    """[테마코드 3][테마명 40바이트(cp949)][종목코드 6][filler 3] — 테마명이 바이트 폭이라
    문자 단위로는 뒤에서부터 자른다."""
    line = line.rstrip("\r\n")
    if len(line) < 12:
        return None
    symbol = line[-9:-3].strip()
    theme_name = line[3:-9].strip()
    if len(symbol) != 6 or not theme_name:
        return None
    return {"theme_code": line[0:3], "theme_name": theme_name, "symbol": symbol}


async def _download(session: aiohttp.ClientSession, filename: str) -> bytes:
    async with session.get(_BASE_URL + filename) as response:
        response.raise_for_status()
        return await response.read()


def _unzip_text(payload: bytes, member: str) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.read(member).decode("cp949")


def _parse_stocks(text: str, market: str) -> List[Dict[str, Any]]:
    return [row for row in (parse_stock_master_line(l, market) for l in text.splitlines()) if row]


def _parse_themes(text: str) -> List[Dict[str, str]]:
    return [row for row in (parse_theme_line(l) for l in text.splitlines()) if row]


async def refresh_stock_master(conn) -> Dict[str, int]:
    """전종목 + 테마 마스터를 받아 전체 교체한다. 테마 파일 실패는 치명적이지 않다
    (테마 후발 소스만 비활성화되고 나머지는 동작) — 기존 테마 테이블을 그대로 둔다."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        stocks: List[Dict[str, Any]] = []
        for market, layout in _LAYOUTS.items():
            payload = await _download(session, layout["file"] + ".zip")
            text = await asyncio.to_thread(_unzip_text, payload, layout["file"])
            stocks.extend(await asyncio.to_thread(_parse_stocks, text, market))

        themes: List[Dict[str, str]] = []
        try:
            payload = await _download(session, "theme_code.mst.zip")
            text = await asyncio.to_thread(_unzip_text, payload, "theme_code.mst")
            themes = await asyncio.to_thread(_parse_themes, text)
        except Exception:
            logger.warning("테마 마스터 다운로드/파싱 실패 - 기존 테마 데이터 유지", exc_info=True)

    if len(stocks) < 1000:
        raise RuntimeError(f"종목 마스터 파싱 결과가 비정상적으로 적음 ({len(stocks)}건) - 교체하지 않음")
    await db.replace_stock_master(conn, stocks, themes)
    return {"stocks": len(stocks), "eligible": sum(1 for s in stocks if not s["is_excluded"]), "themes": len(themes)}
