# core/fundamentals/yfinance_client.py
"""Yahoo Finance (yfinance) 기반 미국 종목 애널리스트 컨센서스 및 리서치 보강 모듈.

네이버 글로벌 API의 데이터가 누락되었거나 중소형 해외 종목의 경우
월가 애널리스트 목표가, 투자의견(Buy/Hold/Sell), 52주 고저 등을 폴백/보강 데이터로 제공한다.
외부 호출 실패 시 예외를 발생시키지 않고 안전하게 빈 dict/list를 반환한다.
"""
import logging
from typing import Any, Dict, List, Optional

import yfinance as yf

from core.utils.symbol_mapper import is_known_etf, is_overseas_symbol

logger = logging.getLogger(__name__)

# yfinance 내부의 HTTP 404 및 quoteSummary 스크래핑 파싱 에러 노이즈 로그를 억제
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def fetch_analyst_targets(ticker: str) -> Dict[str, Any]:
    """미국 종목의 월가 목표주가 및 투자의견 컨센서스를 가져온다 (동기 함수)."""
    clean_ticker = ticker.strip().upper()
    if not is_overseas_symbol(clean_ticker):
        return {}

    try:
        t = yf.Ticker(clean_ticker)
        info = t.info or {}

        target_mean = info.get("targetMeanPrice")
        target_high = info.get("targetHighPrice")
        target_low = info.get("targetLowPrice")
        recomm_mean = info.get("recommendationMean")  # 1.0(Strong Buy) ~ 5.0(Strong Sell)
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")

        w52_high = info.get("fiftyTwoWeekHigh")
        w52_low = info.get("fiftyTwoWeekLow")
        trailing_pe = info.get("trailingPE")
        forward_pe = info.get("forwardPE")
        price_to_book = info.get("priceToBook")

        return {
            "symbol": clean_ticker,
            "target_price_mean": float(target_mean) if target_mean is not None else None,
            "price_target_high": float(target_high) if target_high is not None else None,
            "price_target_low": float(target_low) if target_low is not None else None,
            "recomm_mean": float(recomm_mean) if recomm_mean is not None else None,
            "current_price": float(current_price) if current_price is not None else None,
            "last_close": float(current_price) if current_price is not None else None,
            "w52_high": float(w52_high) if w52_high is not None else None,
            "w52_low": float(w52_low) if w52_low is not None else None,
            "per": float(trailing_pe or forward_pe) if (trailing_pe or forward_pe) else None,
            "pbr": float(price_to_book) if price_to_book is not None else None,
            "source": "yfinance",
        }
    except Exception as e:
        logger.debug(f"yfinance 컨센서스 조회 생략/실패 ({clean_ticker}): {e}")
        return {}


def fetch_upgrades_downgrades(ticker: str, max_items: int = 5) -> List[Dict[str, Any]]:
    """최근 투자은행의 투자의견 상향/하향 및 목표가 변동 이력을 가져온다."""
    clean_ticker = ticker.strip().upper()
    if not is_overseas_symbol(clean_ticker):
        return []

    # ETF(대표지수/배당/테마/단일주식옵션인컴)는 월가 개별 애널리스트 투자의견 변동이 없으므로 조회 생략 (404 방지)
    if is_known_etf(clean_ticker):
        return []

    try:
        t = yf.Ticker(clean_ticker)
        yf_logger = logging.getLogger("yfinance")
        prev_level = yf_logger.level
        try:
            yf_logger.setLevel(logging.CRITICAL)
            df = getattr(t, "upgrades_downgrades", None)
        finally:
            yf_logger.setLevel(prev_level)

        if df is None or df.empty:
            return []

        # 최신순 정렬
        df_sorted = df.sort_index(ascending=False).head(max_items)
        results = []
        for dt_idx, row in df_sorted.iterrows():
            date_str = str(dt_idx)[:10].replace("-", "")
            firm = row.get("Firm") or row.get("firm") or "WallStreet"
            to_grade = row.get("ToGrade") or row.get("toGrade") or ""
            from_grade = row.get("FromGrade") or row.get("fromGrade") or ""
            action = row.get("Action") or row.get("action") or ""
            results.append({
                "broker": str(firm),
                "title": f"[{action.upper()}] {from_grade} -> {to_grade}" if from_grade else f"[{action.upper()}] {to_grade}",
                "date": date_str,
                "read_count": 0,
            })
        return results
    except Exception as e:
        logger.debug(f"yfinance 투자의견 변동 이력 조회 생략/실패 ({clean_ticker}): {e}")
        return []


def fetch_recent_news_yfinance(ticker: str, max_items: int = 5) -> List[Dict[str, Any]]:
    """yfinance를 통한 종목 뉴스 수집 (폴백용 동기 함수)."""
    clean_ticker = ticker.strip().upper()
    if not is_overseas_symbol(clean_ticker):
        return []

    try:
        raw_news = []
        try:
            s = yf.Search(clean_ticker, news_count=max_items)
            raw_news = getattr(s, "news", []) or []
        except Exception:
            pass

        if not raw_news:
            try:
                t = yf.Ticker(clean_ticker)
                raw_news = getattr(t, "news", []) or []
            except Exception:
                pass
        articles = []
        for item in raw_news[:max_items]:
            content = item.get("content") or item
            title = content.get("title") or item.get("title") or ""
            if not title:
                continue
            canonical_url = content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else item.get("link")
            pub_time = content.get("pubDate") or item.get("providerPublishTime")
            # epoch float 변환
            published_at = None
            if isinstance(pub_time, (int, float)):
                published_at = float(pub_time)

            summary = content.get("summary") or item.get("summary") or ""
            articles.append({
                "symbol": clean_ticker,
                "source": "yfinance_news",
                "url": canonical_url or f"https://finance.yahoo.com/quote/{clean_ticker}",
                "title": title,
                "summary": summary,
                "published_at": published_at,
            })
        return articles
    except Exception as e:
        logger.debug(f"yfinance 뉴스 조회 생략/실패 ({clean_ticker}): {e}")
        return []

