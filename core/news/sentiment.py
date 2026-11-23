# core/news/sentiment.py
"""뉴스 수집 -> 중복기사 병합 -> (신규 기사만) LLM 감성분석 -> 시간감쇠 가중평균으로 결합.

signal_engine이 기대하는 Signal 형태({"direction","strength"})로 반환한다.
이미 채점된 기사는 news_cache에서 재사용해 동일 기사에 대한 LLM 재호출을 피한다.
"""
import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db
from core.config import settings
from core.llm.base import LLMProvider
from core.news.etf_mapper import get_etf_profile
from core.news.naver_finance import fetch_recent_news

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 같은 사건을 여러 언론사가 보도했다고 판단하는 제목 토큰 유사도(자카드) 임계치.
# 임베딩 API 없이 가볍게 처리 — 우리 규모(사이클당 최대 몇 건)에는 이 정도로 충분하다.
TITLE_SIMILARITY_THRESHOLD = 0.5

# 뉴스 감성 가중치 반감기(시간) — 오래된 기사가 오늘 기사와 똑같이 반영되지 않도록.
SENTIMENT_HALF_LIFE_HOURS = 12.0

# 뉴스 최대 유효 시간 (시간) - 주말(금요일 장마감 ~ 월요일 개장 약 65시간)을 감안해 72시간으로 설정.
# 72시간 초과 기사는 반감기(12h) 기준 가중치가 1.5% 미만으로 떨어져 단기 매매 판단에 실효성이 없으므로
# Gemini API 호출을 생략하고 중립(0.0)으로 처리한다.
MAX_NEWS_LOOKBACK_HOURS = 72.0


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
    conn: aiosqlite.Connection,
    provider: LLMProvider,
    symbol: str,
    _depth: int = 0,
) -> Optional[Dict[str, Any]]:
    """뉴스가 하나도 없으면 None을 반환한다 (signal_engine은 None이면 기술적 지표만 사용)."""
    clean_sym = symbol.strip().upper()

    # 1. 3계층 ETF 인텔리전스 라우팅 (재귀 호출 깊이 1 초과 방지)
    if _depth == 0:
        etf_profile = await get_etf_profile(conn, clean_sym)
        if etf_profile:
            etf_type = etf_profile.get("type")

            # 1-1. 단일 기초자산 ETF (TSLW -> TSLA, NFLW -> NFLX 등)
            if etf_type == "SINGLE_STOCK":
                underlying = etf_profile.get("underlying")
                if underlying:
                    logger.info(f"ETF '{clean_sym}' ➔ 기초자산 '{underlying}' 뉴스 감성 프록시 상속")
                    underlying_sentiment = await get_symbol_sentiment(
                        conn, provider, underlying, _depth=_depth + 1
                    )
                    if underlying_sentiment:
                        res = dict(underlying_sentiment)
                        res["etf_proxy"] = {
                            "etf_symbol": clean_sym,
                            "type": "SINGLE_STOCK",
                            "underlying": underlying,
                        }
                        return res
                return None

            # 1-2. 특정 테마/섹터 ETF (OZEM -> LLY + NVO, SMH -> NVDA + TSM 등)
            elif etf_type == "THEMATIC":
                anchors = etf_profile.get("anchors") or []
                if anchors:
                    logger.info(f"테마 ETF '{clean_sym}' ➔ 앵커 종목 {anchors} 감성 가중평균 산출")
                    anchor_sentiments = await asyncio.gather(
                        *(get_symbol_sentiment(conn, provider, a, _depth=_depth + 1) for a in anchors)
                    )
                    valid = [s for s in anchor_sentiments if s is not None]
                    if valid:
                        m = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}
                        avg_signed = sum(m.get(s["direction"], 0.0) * s.get("strength", 0.0) for s in valid) / len(valid)
                        direction = "BUY" if avg_signed > 0.15 else "SELL" if avg_signed < -0.15 else "HOLD"
                        strength = min(1.0, abs(avg_signed))
                        all_articles = [art for s in valid for art in s.get("articles") or []]
                        return {
                            "direction": direction,
                            "strength": strength,
                            "articles": all_articles,
                            "etf_proxy": {
                                "etf_symbol": clean_sym,
                                "type": "THEMATIC",
                                "theme": etf_profile.get("theme"),
                                "anchors": anchors,
                                "evaluated_anchors": len(valid),
                            },
                        }
                return None

            # 1-3. 광범위 지수/배당형 ETF (SCHD, SPY, QQQ, SOL미배당 등)
            elif etf_type == "BROAD_INDEX":
                logger.debug(f"ETF '{clean_sym}' 광범위 지수/배당형: 개별 뉴스 감성 제외 (매크로/기술지표 주력)")
                return None

    articles = await asyncio.to_thread(
        fetch_recent_news, symbol, settings.NEWS_MAX_ARTICLES_PER_SYMBOL
    )
    if not articles:
        return None

    articles = _dedupe_articles(articles)

    from core.intel import db as intel_db
    from core.intel.event_deduper import EventDeduper, build_url_alias_key

    deduper = EventDeduper()
    intel_conn = None
    try:
        await intel_db.init_intel_db()
        intel_conn = await intel_db.get_intel_connection()
    except Exception as e:
        logger.debug(f"공유 인텔리전스 DB 연결 생략: {e}")

    is_consumer = getattr(settings, "INTEL_ROLE", "consumer").lower() == "consumer"
    allow_on_demand = bool(getattr(settings, "INTEL_ALLOW_ON_DEMAND_FALLBACK", False))
    market = "KR" if clean_sym.isdigit() else "US"

    weighted_scores: List[tuple] = []  # (score, weight)
    considered: List[Dict[str, Any]] = []  # 재현/디버깅용 원시 스냅샷 (decision_log.context_json에 저장됨)
    try:
        for article in articles:
            pub_at = article.get("published_at")
            age_hours = (time.time() - pub_at) / 3600.0 if pub_at is not None else 0.0
            alias_key = build_url_alias_key(article["url"])

            # 1. 공유 인텔리전스 DB (EventDeduper: 정확 URL + 유사 사건 노드) 최우선 조회
            score: Optional[float] = None
            was_cached = False
            if intel_conn is not None:
                match = await deduper.resolve_or_bind(
                    intel_conn,
                    alias_key=alias_key,
                    symbol=clean_sym,
                    market=market,
                    title=article["title"],
                    source=article.get("source"),
                    published_at=pub_at,
                )
                if match.matched and match.node and match.node.get("sentiment_score") is not None:
                    score = float(match.node["sentiment_score"])
                    was_cached = True

            # 2. 계좌 로컬 news_cache 조회 (하위호환)
            if score is None:
                cached = await db.get_cached_news_sentiment(
                    conn, article["url"], title=article.get("title"), symbol=symbol, published_at=pub_at
                )
                if cached is not None:
                    score = cached["sentiment_score"]
                    was_cached = True

            if score is None:
                if pub_at is not None and age_hours > MAX_NEWS_LOOKBACK_HOURS:
                    score = 0.0
                    reasoning = f"발행일 기준 {int(age_hours)}시간 경과 (유효기간 72시간 초과로 중립 처리)"
                    await db.cache_news_sentiment(
                        conn, symbol, article["source"], article["url"], pub_at,
                        article["title"], score, reasoning,
                    )
                elif is_consumer and not allow_on_demand:
                    # consumer 엔진은 마스터 DB에 아직 없는 신규 기사에 대해 중복 LLM 호출을 하지 않고 스킵
                    continue
                else:
                    result = await provider.analyze_news(symbol, article["title"], article["summary"])
                    score = _signed_score(result)
                    reasoning = result.get("reasoning", "")
                    await db.cache_news_sentiment(
                        conn, symbol, article["source"], article["url"], pub_at,
                        article["title"], score, reasoning,
                    )
                    if intel_conn is not None:
                        await deduper.register_node(
                            intel_conn,
                            alias_key=alias_key,
                            symbol=clean_sym,
                            market=market,
                            title=article["title"],
                            source=article.get("source"),
                            published_at=pub_at,
                            sentiment_direction=result.get("direction", "HOLD"),
                            sentiment_score=score,
                            sentiment_reasoning=reasoning,
                            model_used=getattr(settings, "GEMINI_FAST_MODEL", "gemini"),
                        )

            weight = _recency_weight(pub_at)
            weighted_scores.append((score, weight))
            considered.append({
                "url": article["url"], "title": article["title"], "score": score,
                "weight": weight, "cached": was_cached,
            })
    finally:
        if intel_conn is not None:
            await intel_conn.close()

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
