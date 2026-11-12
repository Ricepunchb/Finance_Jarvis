# core/us_market/client.py
"""Yahoo Finance (yfinance) 기반 미국 주식 전용 데이터 수집 모듈.

증권사 OpenAPI 연결 없이도 모든 미국 주식 및 ETF의 30분봉, 일봉, 실시간 시세,
월가 애널리스트 목표가 및 뉴스를 안정적으로 수집합니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timedelta
import logging
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")
KST_TZ = ZoneInfo("Asia/Seoul")


def get_us_market_status(now_dt: Optional[datetime] = None) -> Dict[str, Any]:
    """현재 미국 정규장, 프리마켓, 애프터마켓, 휴장 여부를 판정한다."""
    now_ny = now_dt.astimezone(NY_TZ) if now_dt else datetime.now(tz=NY_TZ)
    now_kst = now_ny.astimezone(KST_TZ)

    is_weekend = now_ny.weekday() >= 5  # 5: 토, 6: 일
    is_dst = now_ny.dst() != timedelta(0)  # 서머타임 여부 (True: EDT, False: EST)

    t = now_ny.time()
    t_pre_start = dtime(4, 0)
    t_reg_start = dtime(9, 30)
    t_reg_end = dtime(16, 0)
    t_post_end = dtime(20, 0)

    if is_weekend:
        status = "CLOSED"
        status_label = "주말 휴장"
        is_regular_open = False
    elif t_reg_start <= t <= t_reg_end:
        status = "REGULAR"
        status_label = "🟢 정규장 거래 중"
        is_regular_open = True
    elif t_pre_start <= t < t_reg_start:
        status = "PRE_MARKET"
        status_label = "🟡 프리마켓"
        is_regular_open = False
    elif t_reg_end < t <= t_post_end:
        status = "AFTER_HOURS"
        status_label = "🟠 애프터마켓"
        is_regular_open = False
    else:
        status = "CLOSED"
        status_label = "장 마감"
        is_regular_open = False

    kst_reg_open = "22:30" if is_dst else "23:30"
    kst_reg_close = "05:00" if is_dst else "06:00"

    return {
        "status": status,
        "status_label": status_label,
        "is_regular_open": is_regular_open,
        "is_weekend": is_weekend,
        "is_dst": is_dst,
        "ny_time_str": now_ny.strftime("%Y-%m-%d %H:%M:%S EDT" if is_dst else "%Y-%m-%d %H:%M:%S EST"),
        "kst_time_str": now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
        "kst_reg_hours": f"{kst_reg_open} ~ {kst_reg_close} KST",
    }


def _fetch_naver_quote_fallback(clean_sym: str) -> Optional[Dict[str, Any]]:
    """yfinance 레이트리밋(HTTP 429) 또는 장애 시 네이버 해외주식 API로 현재가를 조회한다."""
    import requests
    from core.utils.symbol_mapper import ticker_to_reuters_code

    rc = ticker_to_reuters_code(clean_sym)
    for cand_rc in [rc, clean_sym, f"{clean_sym}.O", f"{clean_sym}.N"]:
        try:
            resp = requests.get(
                f"https://api.stock.naver.com/stock/{cand_rc}/basic",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5,
            )
            if resp.status_code == 200 and resp.text.strip():
                data = resp.json() or {}
                raw_p = str(data.get("closePrice") or data.get("closePriceRaw") or "").replace(",", "")
                if not raw_p:
                    continue
                price = float(raw_p)
                raw_chg = str(data.get("compareToPreviousClosePrice") or "0").replace(",", "")
                raw_pct = str(data.get("fluctuationsRatio") or "0").replace(",", "")
                chg = float(raw_chg) if raw_chg else 0.0
                chg_pct = float(raw_pct) if raw_pct else 0.0
                prev_close = round(price - chg, 4) if price > chg else price
                return {
                    "symbol": clean_sym,
                    "name": data.get("stockNameEng") or data.get("stockName") or clean_sym,
                    "sector": "US Equity",
                    "price": price,
                    "prev_close": prev_close,
                    "change": round(chg, 4),
                    "change_pct": round(chg_pct, 2),
                    "w52_high": None,
                    "w52_low": None,
                    "market_cap": None,
                    "currency": "USD",
                }
        except Exception:
            continue
    return None


def _fetch_naver_daily_bars_fallback(clean_sym: str) -> pd.DataFrame:
    """yfinance 레이트리밋(HTTP 429) 시 네이버 해외주식 일봉 차트 API로 폴백한다."""
    import requests
    from core.utils.symbol_mapper import ticker_to_reuters_code

    rc = ticker_to_reuters_code(clean_sym)
    start_str = (datetime.now() - timedelta(days=200)).strftime("%Y%m%d0000")
    end_str = (datetime.now() + timedelta(days=2)).strftime("%Y%m%d0000")

    for cand_rc in [rc, clean_sym, f"{clean_sym}.O", f"{clean_sym}.N"]:
        try:
            resp = requests.get(
                f"https://api.stock.naver.com/chart/foreign/item/{cand_rc}/day?startDateTime={start_str}&endDateTime={end_str}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5,
            )
            if resp.status_code == 200 and resp.text.strip():
                rows = resp.json()
                if isinstance(rows, list) and len(rows) > 0:
                    df = pd.DataFrame(rows)
                    df = df.rename(columns={
                        "localDate": "date",
                        "openPrice": "open",
                        "highPrice": "high",
                        "lowPrice": "low",
                        "closePrice": "close",
                        "accumulatedTradingVolume": "volume",
                    })
                    for c in ("open", "high", "low", "close", "volume"):
                        if c in df.columns:
                            df[c] = pd.to_numeric(df[c], errors="coerce")
                    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
                    df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
                    return df
        except Exception:
            continue
    return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])


def _fetch_quote_sync(symbol: str) -> Dict[str, Any]:
    """단일 종목 현재가 및 기본 정보 동기 조회."""
    clean_sym = symbol.strip().upper()
    try:
        t = yf.Ticker(clean_sym)
        fast = t.fast_info

        last_price = getattr(fast, "last_price", None)
        prev_close = getattr(fast, "previous_close", None)

        # 폴백 시도
        if last_price is None or prev_close is None:
            hist = t.history(period="2d")
            if not hist.empty:
                last_price = float(hist["Close"].iloc[-1])
                prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last_price

        if last_price is None:
            fb = _fetch_naver_quote_fallback(clean_sym)
            return fb if fb else {"symbol": clean_sym, "error": "가격 정보 없음"}

        change = last_price - prev_close if prev_close else 0.0
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        w52_high = getattr(fast, "year_high", None)
        w52_low = getattr(fast, "year_low", None)
        mkt_cap = getattr(fast, "market_cap", None)
        currency = getattr(fast, "currency", "USD")

        return {
            "symbol": clean_sym,
            "name": clean_sym,
            "sector": "US Equity",
            "price": float(last_price),
            "prev_close": float(prev_close) if prev_close else float(last_price),
            "change": round(float(change), 4),
            "change_pct": round(float(change_pct), 2),
            "w52_high": float(w52_high) if w52_high else None,
            "w52_low": float(w52_low) if w52_low else None,
            "market_cap": float(mkt_cap) if mkt_cap else None,
            "currency": currency or "USD",
        }
    except Exception as e:
        fb = _fetch_naver_quote_fallback(clean_sym)
        if fb:
            return fb
        logger.warning(f"yfinance 현재가 조회 오류 ({clean_sym}): {e}")
        return {"symbol": clean_sym, "error": str(e)}


async def get_us_quote(symbol: str) -> Dict[str, Any]:
    """비동기 현재가 및 기본 정보 조회."""
    return await asyncio.to_thread(_fetch_quote_sync, symbol)


def _fetch_30m_bars_sync(symbol: str, period: str = "1mo") -> pd.DataFrame:
    """30분봉 OHLCV 수집.

    core/indicators.compute_technical_signal() 표준 포맷(date, open, high, low, close, volume) 반환.
    """
    clean_sym = symbol.strip().upper()
    try:
        t = yf.Ticker(clean_sym)
        df = t.history(interval="30m", period=period)
        if df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        df = df.reset_index()
        # Datetime 또는 Date 컬럼 대응
        date_col = "Datetime" if "Datetime" in df.columns else ("Date" if "Date" in df.columns else df.columns[0])
        df = df.rename(columns={
            date_col: "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).reset_index(drop=True)
        return df
    except Exception as e:
        logger.warning(f"yfinance 30분봉 수집 오류 ({clean_sym}): {e}")
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])


async def get_us_30m_bars(symbol: str, period: str = "1mo") -> pd.DataFrame:
    """비동기 30분봉 OHLCV 수집."""
    return await asyncio.to_thread(_fetch_30m_bars_sync, symbol, period)


def _fetch_daily_bars_sync(symbol: str, period: str = "3mo") -> pd.DataFrame:
    """일봉 OHLCV 수집.

    core/indicators.compute_technical_signal() 표준 포맷(date, open, high, low, close, volume) 반환.
    """
    clean_sym = symbol.strip().upper()
    try:
        t = yf.Ticker(clean_sym)
        df = t.history(interval="1d", period=period)
        if df.empty:
            return _fetch_naver_daily_bars_fallback(clean_sym)

        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else ("Datetime" if "Datetime" in df.columns else df.columns[0])
        df = df.rename(columns={
            date_col: "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"]).reset_index(drop=True)
        return df
    except Exception as e:
        fb_df = _fetch_naver_daily_bars_fallback(clean_sym)
        if not fb_df.empty:
            return fb_df
        logger.warning(f"yfinance 일봉 수집 오류 ({clean_sym}): {e}")
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])


async def get_us_daily_bars(symbol: str, period: str = "3mo") -> pd.DataFrame:
    """비동기 일봉 OHLCV 수집."""
    return await asyncio.to_thread(_fetch_daily_bars_sync, symbol, period)


def _fetch_consensus_and_news_sync(symbol: str) -> Dict[str, Any]:
    """월가 컨센서스 목표주가 및 최근 뉴스 수집.

    - 단일 기초자산 ETF(TSLW -> TSLA 등): 원본 주식의 목표가 및 뉴스 100% 프록시 상속
    - 테마/섹터 ETF(OZEM -> LLY + NVO, SOXL -> NVDA + AVGO + AMD 등): 핵심 앵커 기업 전체의
      목표가 괴리율 가중평균 및 기업별 대표 뉴스를 종합 수집
    """
    clean_sym = symbol.strip().upper()
    try:
        from core.news.etf_mapper import resolve_etf_profile_sync
        profile = resolve_etf_profile_sync(clean_sym)
        underlying: Optional[str] = None
        is_etf_proxy = False
        target_symbols = [clean_sym]
        is_thematic = False

        if profile:
            etf_type = profile.get("type")
            if etf_type == "SINGLE_STOCK" and profile.get("underlying"):
                target_sym = profile["underlying"].strip().upper()
                target_symbols = [target_sym]
                underlying = target_sym
                is_etf_proxy = True
            elif etf_type == "THEMATIC" and profile.get("anchors"):
                anchors = [a.strip().upper() for a in profile["anchors"] if a]
                if anchors:
                    target_symbols = anchors
                    underlying = ", ".join(anchors)
                    is_etf_proxy = True
                    is_thematic = True

        KNOWN_INDEX_ETFS = {
            "VOO", "SPY", "QQQ", "SCHD", "SGOV", "SHV", "BIL", "GLD", "IAU",
            "TLT", "IEF", "BND", "AGG", "TSLW", "IVV", "VTI"
        }

        # 단일 종목 / SINGLE_STOCK인 경우
        if not is_thematic:
            target_sym = target_symbols[0]
            t = yf.Ticker(target_sym)
            info = {}
            if target_sym not in KNOWN_INDEX_ETFS:
                try:
                    info = t.info or {}
                except Exception:
                    info = {}

            target_mean = info.get("targetMeanPrice")
            target_high = info.get("targetHighPrice")
            target_low = info.get("targetLowPrice")
            current_price = info.get("currentPrice") or info.get("regularMarketPrice")
            recomm_key = info.get("recommendationKey", "")
            recomm_mean = info.get("recommendationMean")  # 1.0(Strong Buy) ~ 5.0(Strong Sell)

            target_gap_pct = None
            if target_mean and current_price and current_price > 0:
                target_gap_pct = round(((target_mean - current_price) / current_price) * 100, 2)

            # 뉴스: yf.Search 신규 엔드포인트 우선 수집 (t.news 장애 및 YFDataException 격리)
            raw_news = []
            try:
                search_obj = yf.Search(target_sym, news_count=5)
                raw_news = getattr(search_obj, "news", []) or []
            except Exception:
                pass

            if not raw_news:
                try:
                    raw_news = getattr(t, "news", []) or []
                except Exception:
                    pass

            articles: List[Dict[str, Any]] = []
            for item in raw_news[:5]:
                content = item.get("content") or item
                title = content.get("title") or item.get("title") or ""
                if not title:
                    continue
                link = content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else item.get("link")
                pub_time = content.get("pubDate") or item.get("providerPublishTime")
                publisher = content.get("provider", {}).get("displayName") or item.get("publisher") or "Wall Street News"
                articles.append({
                    "title": title,
                    "publisher": publisher,
                    "link": link or "",
                    "published_at": pub_time,
                })

            return {
                "symbol": clean_sym,
                "target_mean": float(target_mean) if target_mean else None,
                "target_high": float(target_high) if target_high else None,
                "target_low": float(target_low) if target_low else None,
                "target_gap_pct": target_gap_pct,
                "recommendation_key": recomm_key,
                "recommendation_mean": float(recomm_mean) if recomm_mean else None,
                "news": articles,
                "underlying": underlying,
                "is_etf_proxy": is_etf_proxy,
            }

        # 테마/섹터(THEMATIC) 멀티 앵커인 경우 (예: OZEM -> LLY + NVO, SOXL -> NVDA + AVGO + AMD)
        gaps: List[float] = []
        recomm_means: List[float] = []
        articles: List[Dict[str, Any]] = []
        seen_titles = set()

        for anchor in target_symbols:
            try:
                t = yf.Ticker(anchor)
                info = {}
                if anchor not in KNOWN_INDEX_ETFS:
                    try:
                        info = t.info or {}
                    except Exception:
                        info = {}
                tm = info.get("targetMeanPrice")
                cp = info.get("currentPrice") or info.get("regularMarketPrice")
                if tm and cp and cp > 0:
                    gaps.append(((tm - cp) / cp) * 100)
                rm = info.get("recommendationMean")
                if rm:
                    recomm_means.append(float(rm))

                # 앵커별 최신 뉴스 2건 수집 (yf.Search 우선, 예외 격리)
                raw_news = []
                try:
                    search_obj = yf.Search(anchor, news_count=3)
                    raw_news = getattr(search_obj, "news", []) or []
                except Exception:
                    pass

                if not raw_news:
                    try:
                        raw_news = getattr(t, "news", []) or []
                    except Exception:
                        pass

                for item in raw_news[:2]:
                    content = item.get("content") or item
                    raw_title = content.get("title") or item.get("title") or ""
                    if not raw_title or raw_title in seen_titles:
                        continue
                    seen_titles.add(raw_title)
                    link = content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else item.get("link")
                    pub_time = content.get("pubDate") or item.get("providerPublishTime")
                    publisher = content.get("provider", {}).get("displayName") or item.get("publisher") or "Wall Street News"
                    articles.append({
                        "title": f"[{anchor}] {raw_title}",
                        "publisher": publisher,
                        "link": link or "",
                        "published_at": pub_time,
                    })
            except Exception as anchor_err:
                logger.warning(f"테마 ETF '{clean_sym}' 앵커 '{anchor}' 수집 오류: {anchor_err}")

        avg_gap_pct = round(sum(gaps) / len(gaps), 2) if gaps else None
        avg_recomm_mean = round(sum(recomm_means) / len(recomm_means), 2) if recomm_means else None
        if avg_recomm_mean:
            recomm_key = "strong_buy" if avg_recomm_mean <= 1.5 else "buy" if avg_recomm_mean <= 2.5 else "hold"
        else:
            recomm_key = ""

        # 대표 1위 앵커의 목표가
        first_info = {}
        if target_symbols and target_symbols[0] not in KNOWN_INDEX_ETFS:
            try:
                first_t = yf.Ticker(target_symbols[0])
                first_info = first_t.info or {}
            except Exception:
                pass

        return {
            "symbol": clean_sym,
            "target_mean": float(first_info.get("targetMeanPrice")) if first_info.get("targetMeanPrice") else None,
            "target_high": float(first_info.get("targetHighPrice")) if first_info.get("targetHighPrice") else None,
            "target_low": float(first_info.get("targetLowPrice")) if first_info.get("targetLowPrice") else None,
            "target_gap_pct": avg_gap_pct,
            "recommendation_key": recomm_key,
            "recommendation_mean": avg_recomm_mean,
            "news": articles,
            "underlying": underlying,
            "is_etf_proxy": True,
        }
    except Exception as e:
        logger.warning(f"yfinance 컨센서스/뉴스 수집 오류 ({clean_sym}): {e}")
        return {
            "symbol": clean_sym,
            "target_mean": None,
            "target_high": None,
            "target_low": None,
            "target_gap_pct": None,
            "recommendation_key": "",
            "recommendation_mean": None,
            "news": [],
            "underlying": None,
            "is_etf_proxy": False,
        }


async def get_us_consensus_and_news(symbol: str) -> Dict[str, Any]:
    """비동기 월가 컨센서스 및 뉴스 수집."""
    return await asyncio.to_thread(_fetch_consensus_and_news_sync, symbol)

