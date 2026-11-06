# core/us_market/surprise.py
"""글로벌 뉴스 인텔리전스 및 Surprise 점수 산출 & 군집화 모듈.

역할:
1. 최신 미국/글로벌 뉴스 수집 (워치리스트 종목 및 주요 지수)
2. 동일 사건(Event)에 대한 다수 언론사 기사 자동 군집화(Clustering) 및 중복 제거
3. 시장 기대치(Consensus) 괴리도 + 실시간 주가 반응 결합 Surprise 점수(0~100점) 산출
4. 이모지가 배제된 정갈한 3문장 이내 핵심 요약 및 텔레그램 브리핑 메시지 생성
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from core import db
from core.config import settings
from core.llm.base import LLMProvider

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 불용어 목록 (자카드 유사도 산출용)
_STOPWORDS = {
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or", "is", "are",
    "was", "were", "with", "as", "by", "from", "it", "its", "that", "this", "after",
    "before", "about", "says", "said", "new", "stock", "shares", "inc", "corp", "co",
    "오늘", "미국", "증시", "주가", "관련", "기사", "보도", "발표",
}


class NewsArticle(BaseModel):
    symbol: str
    title: str
    summary: str
    url: str = ""
    source: str = "Yahoo Finance"
    published_at: float = Field(default_factory=time.time)


class NewsCluster(BaseModel):
    cluster_id: str
    symbol: str
    anchor: NewsArticle
    articles: List[NewsArticle]
    sources: List[str]
    count: int = 1


class SurpriseAnalysisResult(BaseModel):
    surprise_score: int = Field(description="시장 기대치(Consensus)와의 괴리도 (0~100점)")
    impact_type: str = Field(description="호재 또는 악재. (호재 / 악재 / 중립)")
    affected_sector_theme: str = Field(description="영향 받는 섹터 또는 테마 (예: 반도체 및 AI 하드웨어, 미국 단기 국채 금리)")
    summary_sentences: List[str] = Field(description="핵심 요약 문장 목록 (최대 3문장, 각 문장은 명확하고 완결된 한국어)")
    rationale: str = Field(description="서프라이즈 점수 부여 이유 한 문장")


def _clean_text_for_tokens(text: str) -> Set[str]:
    """기사 제목에서 핵심 단어 및 숫자/퍼센트 토큰 추출."""
    words = re.findall(r"[\w%$\.]+", text.lower())
    tokens = {w for w in words if len(w) > 1 and w not in _STOPWORDS}
    return tokens


def _compute_jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def cluster_news_articles(
    articles: List[NewsArticle],
    similarity_threshold: float = 0.38,
    time_window_hours: float = 6.0,
) -> List[NewsCluster]:
    """동일 종목 및 유사 시간대에 보도된 같은 사건(Event) 기사들을 단일 클러스터로 군집화한다."""
    if not articles:
        return []

    # 1. 시간순 정렬 (최신 기사 우선)
    sorted_articles = sorted(articles, key=lambda a: a.published_at, reverse=True)

    clusters: List[NewsCluster] = []
    assigned_indices: Set[int] = set()

    for i, lead in enumerate(sorted_articles):
        if i in assigned_indices:
            continue

        assigned_indices.add(i)
        lead_tokens = _clean_text_for_tokens(lead.title)
        cluster_articles = [lead]
        cluster_sources = [lead.source] if lead.source else ["News"]

        for j in range(i + 1, len(sorted_articles)):
            if j in assigned_indices:
                continue

            cand = sorted_articles[j]
            # 같은 종목이어야 함
            if cand.symbol.upper() != lead.symbol.upper():
                continue

            # 시간차가 지정된 윈도우 이내여야 함
            time_diff_hours = abs(lead.published_at - cand.published_at) / 3600.0
            if time_diff_hours > time_window_hours:
                continue

            cand_tokens = _clean_text_for_tokens(cand.title)
            sim = _compute_jaccard_similarity(lead_tokens, cand_tokens)

            # 유사도가 임계치 이상이거나 제목이 부분 일치하면 동일 사건으로 묶음
            if sim >= similarity_threshold or (cand.title in lead.title) or (lead.title in cand.title):
                assigned_indices.add(j)
                cluster_articles.append(cand)
                if cand.source and cand.source not in cluster_sources:
                    cluster_sources.append(cand.source)

        # 클러스터 ID 생성 (종목명 + 날짜 + 핵심 토큰 해시)
        anchor_title = lead.title.strip()
        hash_seed = f"{lead.symbol}_{anchor_title[:40]}_{datetime.fromtimestamp(lead.published_at, tz=KST).strftime('%Y%m%d')}"
        cluster_id = hashlib.sha256(hash_seed.encode("utf-8")).hexdigest()[:16]

        clusters.append(
            NewsCluster(
                cluster_id=cluster_id,
                symbol=lead.symbol.upper(),
                anchor=lead,
                articles=cluster_articles,
                sources=cluster_sources,
                count=len(cluster_articles),
            )
        )

    return clusters


async def analyze_news_surprise(
    cluster: NewsCluster,
    llm_provider: Optional[LLMProvider] = None,
    current_price_change_pct: Optional[float] = None,
) -> SurpriseAnalysisResult:
    """클러스터링된 대표 뉴스를 바탕으로 기대치 괴리도 및 Surprise 점수를 산출한다."""
    anchor = cluster.anchor
    title = anchor.title
    summary = anchor.summary or anchor.title
    sym = cluster.symbol

    # 여러 언론사의 제목들을 모아 맥락을 보강
    other_titles = [a.title for a in cluster.articles if a.title != title][:3]
    other_context = f"\n(동일 이슈 추가 보도: {'; '.join(other_titles)})" if other_titles else ""

    if llm_provider is not None:
        try:
            prompt = (
                f"다음은 미국 주식/글로벌 시장의 종목 '{sym}' 관련 뉴스다.\n"
                f"이 뉴스가 '시장 컨센서스(기대치)를 얼마나 벗어났는지(Surprise)' 분석하라.\n\n"
                f"기사 제목: {title}\n"
                f"기사 내용 요약: {summary}{other_context}\n\n"
                f"[분석 및 채점 기준]\n"
                f"1. surprise_score (0~100점): 시장이 미리 예상하지 못한 충격이나 괴리의 크기.\n"
                f"   - 0~49: 일상 시황, 칼럼, 기선반영된 단순 뉴스 (임팩트 미미)\n"
                f"   - 50~69: 무난한 실적 부합, 통상적인 계약 공시, 소폭의 목표가 변경\n"
                f"   - 70~84: 유의미한 서프라이즈 (실적 대폭 상회/하회, 가이던스 수정, 대형 독점 계약, 예상 밖 규제)\n"
                f"   - 85~100: 초대형 쇼크/블랙스완 (빅스텝 금리 결정, 수출 전면 금지, 초대형 M&A, 분식회계)\n"
                f"2. impact_type: 반드시 '호재', '악재', '중립' 중 하나.\n"
                f"3. affected_sector_theme: 영향을 받는 산업 섹터 또는 테마 (예: 반도체 및 AI 하드웨어, 미국 배당성장 가치주, 단기 국채 금리)\n"
                f"4. summary_sentences: 핵심 요약 문장 최대 3개 (각 문장은 이모지 없이 한국어 완결형 문장으로 간결 명료하게 작성).\n"
                f"5. 모든 출력에서 이모지(그림 문자)는 절대 사용하지 말 것."
            )
            # GeminiProvider를 통한 구조화 출력 생성
            if hasattr(llm_provider, "_generate"):
                parsed = await llm_provider._generate(prompt, SurpriseAnalysisResult, temperature=0.2)
                # 최대 3문장 제한 준수
                sentences = [s.strip() for s in parsed.summary_sentences if s.strip()][:3]
                if not sentences:
                    sentences = [title]

                base_score = max(0, min(100, int(parsed.surprise_score)))

                # 실시간 주가 반응이 있으면 결합
                if current_price_change_pct is not None:
                    abs_change = abs(current_price_change_pct)
                    if abs_change >= 4.0:
                        base_score = min(100, max(base_score, int(base_score * 0.7 + 90 * 0.3)))
                    elif abs_change >= 2.0:
                        base_score = min(100, max(base_score, int(base_score * 0.8 + 75 * 0.2)))

                return SurpriseAnalysisResult(
                    surprise_score=base_score,
                    impact_type=parsed.impact_type if parsed.impact_type in ("호재", "악재", "중립") else "중립",
                    affected_sector_theme=parsed.affected_sector_theme or "글로벌 거시경제",
                    summary_sentences=sentences,
                    rationale=parsed.rationale or "시장 기대치 대비 괴리도 분석 완료",
                )
        except Exception as e:
            logger.warning(f"'{sym}' 뉴스 서프라이즈 LLM 분석 실패 - 휴리스틱 대체: {e}")

    # Fallback 휴리스틱 분석 (LLM 미연결 또는 장애 시)
    title_lower = title.lower()
    positive_words = ["beats", "surge", "record", "upgrade", "outperform", "soar", "profit", "jump", "상회", "급등", "수주"]
    negative_words = ["misses", "plunge", "downgrade", "probe", "lawsuit", "cut", "warning", "ban", "하회", "급락", "규제", "소송"]

    pos_hit = any(w in title_lower for w in positive_words)
    neg_hit = any(w in title_lower for w in negative_words)

    if pos_hit and not neg_hit:
        impact = "호재"
        score = 72
    elif neg_hit and not pos_hit:
        impact = "악재"
        score = 74
    else:
        impact = "중립"
        score = 45

    if current_price_change_pct and abs(current_price_change_pct) >= 3.0:
        score = min(100, score + 15)

    return SurpriseAnalysisResult(
        surprise_score=score,
        impact_type=impact,
        affected_sector_theme=f"{sym} 관련 산업 및 테마",
        summary_sentences=[title[:120]],
        rationale="키워드 및 가격 변동 기반 정량 산출",
    )


def fetch_latest_us_news(symbol: str, count: int = 8) -> List[NewsArticle]:
    """yfinance Search API를 통해 종목 관련 최신 외신 뉴스(최대 count편)를 수집하여 NewsArticle 목록으로 반환."""
    import yfinance as yf
    clean_sym = symbol.strip().upper()
    articles: List[NewsArticle] = []
    try:
        s = yf.Search(clean_sym, news_count=count)
        raw_news = getattr(s, "news", []) or []
        for item in raw_news:
            content = item.get("content") or item
            title = (content.get("title") or item.get("title") or "").strip()
            if not title:
                continue
            summary = (content.get("summary") or item.get("summary") or title).strip()
            link = content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else (item.get("link") or "")
            pub_time = content.get("pubDate") or item.get("providerPublishTime")
            if isinstance(pub_time, (int, float)):
                pub_ts = float(pub_time)
            elif isinstance(pub_time, str):
                try:
                    pub_ts = datetime.fromisoformat(pub_time.replace("Z", "+00:00")).timestamp()
                except Exception:
                    pub_ts = time.time()
            else:
                pub_ts = time.time()
            publisher = content.get("provider", {}).get("displayName") or item.get("publisher") or "Yahoo Finance"

            articles.append(
                NewsArticle(
                    symbol=clean_sym,
                    title=title,
                    summary=summary,
                    url=str(link),
                    source=str(publisher),
                    published_at=pub_ts,
                )
            )
    except Exception as e:
        logger.warning(f"'{clean_sym}' 최신 뉴스 수집 중 오류: {e}")
    return articles


EMOJI_PATTERN = re.compile(
    r"("
    r"[\U0001F600-\U0001F64F]"  # emoticons
    r"|[\U0001F300-\U0001F5FF]"  # misc symbols & pictographs
    r"|[\U0001F680-\U0001F6FF]"  # transport & map
    r"|[\U0001F1E0-\U0001F1FF]"  # flags
    r"|[\U0001F900-\U0001F9FF]"  # supplemental symbols
    r"|[\U0001FA00-\U0001FAFF]"  # symbols & pictographs extended-A
    r"|[\u2600-\u26FF]"          # misc symbols
    r"|[\u2700-\u27BF]"          # dingbats
    r"|[\u2300-\u23FF]"          # misc technical (⏰ 등)
    r"|[\u2B50\u2B55]"
    r"|[\uFE0E\uFE0F]"           # variation selectors
    r"|[\u200D]"                 # zero width joiner
    r")+",
    flags=re.UNICODE,
)


def strip_all_emojis(text: str) -> str:
    """텍스트에서 유니코드 이모지 및 특수 그림 기호를 한글 손상 없이 100% 제거."""
    if not text:
        return ""
    cleaned = EMOJI_PATTERN.sub("", text)
    return cleaned.strip()


def format_news_surprise_telegram_message(
    symbol: str,
    company_name: str,
    analysis: SurpriseAnalysisResult,
    sources: List[str],
    cluster_count: int = 1,
    published_ts: Optional[float] = None,
) -> str:
    """이모지를 완전히 배제하고 최대 3문장 요약, 영향 섹터/테마, 호재/악재, Surprise 점수를 담은 정갈한 텔레그램 메시지."""
    now_kst = datetime.fromtimestamp(published_ts or time.time(), tz=KST).strftime("%m-%d %H:%M KST")
    name_str = f" ({company_name})" if company_name and company_name != symbol else ""

    src_label = ", ".join(sources[:2]) if sources else "외신 종합"
    if cluster_count > 1:
        src_label += f" (외 {cluster_count - 1}개 매체)"

    summary_lines = []
    for idx, s in enumerate(analysis.summary_sentences[:3], 1):
        clean_s = strip_all_emojis(s).strip()
        if clean_s:
            summary_lines.append(f"{idx}. {clean_s}")

    summary_block = "\n".join(summary_lines) if summary_lines else "1. 상세 보도 내용 요약 없음."

    clean_sector = strip_all_emojis(analysis.affected_sector_theme)
    clean_impact = strip_all_emojis(analysis.impact_type)

    message = (
        f"[글로벌 뉴스 서프라이즈]\n"
        f"- 대상 종목: {symbol}{name_str}\n"
        f"- Surprise 점수: {analysis.surprise_score}점\n"
        f"- 시장 영향: {clean_impact}\n"
        f"- 영향 섹터/테마: {clean_sector}\n\n"
        f"[핵심 요약]\n"
        f"{summary_block}\n\n"
        f"- 관련 출처: {src_label}\n"
        f"- 기준 시각: {now_kst}"
    )

    # 최종 안전장치: 전체 메시지에서 혹시 남아있는 이모지 재확인 제거
    return strip_all_emojis(message)
