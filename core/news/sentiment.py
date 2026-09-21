# core/news/sentiment.py
"""뉴스 수집 -> 중복기사 병합 -> (신규 기사만) LLM 감성분석 -> 시간감쇠 가중평균으로 결합.

signal_engine이 기대하는 Signal 형태({"direction","strength"})로 반환한다.
이미 채점된 기사는 news_cache에서 재사용해 동일 기사에 대한 LLM 재호출을 피한다.
"""
import asyncio
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db
from core.config import settings
from core.llm.base import LLMProvider
from core.news.naver_finance import fetch_recent_news

KST = ZoneInfo("Asia/Seoul")

# 같은 사건을 여러 언론사가 보도했다고 판단하는 제목 토큰 유사도(자카드) 임계치.
# 임베딩 API 없이 가볍게 처리 — 우리 규모(사이클당 최대 몇 건)에는 이 정도로 충분하다.
TITLE_SIMILARITY_THRESHOLD = 0.5

# 뉴스 감성 가중치 반감기(시간) — 오래된 기사가 오늘 기사와 똑같이 반영되지 않도록.
SENTIMENT_HALF_LIFE_HOURS = 12.0


def _signed_score(result: Dict[str, Any]) -> float:
    m = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}
    return m.get(result.get("direction", "HOLD"), 0.0) * result.get("strength", 0.0)


def _title_tokens(title: str) -> set:
    return set(re.findall(r"[\w가-힣]+", title.lower()))


def _titles_similar(a: str, b: str) -> bool:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= TITLE_SIMILARITY_THRESHOLD


def _same_day_kst(ts_a: float, ts_b: float) -> bool:
    return (
        datetime.fromtimestamp(ts_a, tz=KST).date()
        == datetime.fromtimestamp(ts_b, tz=KST).date()
    )


def _dedupe_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """서로 다른 언론사가 같은 사건을 보도하면 하나로 합쳐 감성 점수의 중복가중을 막는다.

    (사이클당 최대 몇 건 규모라 O(n^2) 비교로도 충분하다.)
    """
    representatives: List[Dict[str, Any]] = []
    for article in articles:
        duplicate_of_existing = any(
            article.get("published_at") is not None
            and rep.get("published_at") is not None
            and _same_day_kst(article["published_at"], rep["published_at"])
            and _titles_similar(article["title"], rep["title"])
            for rep in representatives
        )
        if not duplicate_of_existing:
            representatives.append(article)
    return representatives


def _recency_weight(published_at: Optional[float]) -> float:
    if published_at is None:
        return 1.0
    age_hours = max(0.0, (time.time() - published_at) / 3600.0)
    return 0.5 ** (age_hours / SENTIMENT_HALF_LIFE_HOURS)


async def get_symbol_sentiment(
    conn: aiosqlite.Connection, provider: LLMProvider, symbol: str
) -> Optional[Dict[str, Any]]:
    """뉴스가 하나도 없으면 None을 반환한다 (signal_engine은 None이면 기술적 지표만 사용)."""
    articles = await asyncio.to_thread(
        fetch_recent_news, symbol, settings.NEWS_MAX_ARTICLES_PER_SYMBOL
    )
    if not articles:
        return None

    articles = _dedupe_articles(articles)

    weighted_scores: List[tuple] = []  # (score, weight)
    considered: List[Dict[str, Any]] = []  # 재현/디버깅용 원시 스냅샷 (decision_log.context_json에 저장됨)
    for article in articles:
        cached = await db.get_cached_news_sentiment(conn, article["url"])
        was_cached = cached is not None
        if cached is not None:
            score = cached["sentiment_score"]
        else:
            result = await provider.analyze_news(symbol, article["title"], article["summary"])
            score = _signed_score(result)
            await db.cache_news_sentiment(
                conn, symbol, article["source"], article["url"], article["published_at"],
                article["title"], score, result.get("reasoning", ""),
            )
        weight = _recency_weight(article.get("published_at"))
        weighted_scores.append((score, weight))
        considered.append({
            "url": article["url"], "title": article["title"], "score": score,
            "weight": weight, "cached": was_cached,
        })

    total_weight = sum(w for _, w in weighted_scores)
    if total_weight <= 0:
        return None
    avg = sum(score * w for score, w in weighted_scores) / total_weight
    direction = "BUY" if avg > 0.15 else "SELL" if avg < -0.15 else "HOLD"
    return {
        "direction": direction,
        "strength": min(1.0, abs(avg)),
        "articles": considered,  # signal_engine은 무시하고 direction/strength만 씀
    }
