# core/intel/event_deduper.py
"""2단계 뉴스 사건 노드(Event Node) 중복 제거 및 Alias 병합 엔진.

동작 순서:
1. 정확 조회(Exact Lookup): `cluster_id` 또는 기사 `url` 해시(`alias_key`)로
   `news_cluster_aliases`를 먼저 조회해 이미 분석된 노드가 있으면 즉시 반환 (LLM 0회).
2. 유사 사건 노드 병합(Event Node Merge): `alias_key`는 새롭더라도 최근 48시간 내
   동일 종목의 기존 사건 노드(`news_event_nodes`)와 제목 토큰 유사도(Jaccard + 핵심 토픽)가
   임계치 이상이면, LLM 호출 없이 새 `alias_key`를 기존 `node_id`에 묶어(bind) 재사용한다.
3. 신규 사건 등록: 1·2단계를 모두 통과한 완전히 새로운 사건만 분석 후 노드와 Alias를 저장한다.
   (75점 미만 저점수 뉴스도 노드로 저장하여 30분 주기 재분석을 원천 차단한다.)
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

import aiosqlite

from core.intel import db as intel_db

# 영문 + 한국어 공통 불용어
_STOPWORDS: Set[str] = {
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or", "is", "are",
    "was", "were", "with", "as", "by", "from", "it", "its", "that", "this", "after",
    "before", "about", "says", "said", "new", "stock", "shares", "inc", "corp", "co",
    "today", "market", "live", "coverage", "update", "updates", "breaking", "report",
    "오늘", "미국", "증시", "주가", "관련", "기사", "보도", "발표", "속보", "특징주", "마감", "장중",
}

_SYNONYMS: Dict[str, str] = {
    "probes": "probe", "probing": "probe", "investigates": "probe",
    "investigating": "probe", "investigation": "probe", "inquiry": "probe",
    "slump": "drop", "drops": "drop", "plunges": "drop", "falls": "drop",
    "tumbles": "drop", "sinks": "drop", "craters": "drop",
    "surges": "rally", "soars": "rally", "jumps": "rally", "rallies": "rally",
    "lawsuits": "lawsuit", "sues": "lawsuit", "suing": "lawsuit",
    "earnings": "earning", "guidance": "guidance", "tariffs": "tariff",
    "급락": "하락", "폭락": "하락", "추락": "하락",
    "급등": "상승", "폭등": "상승", "강세": "상승",
}

_HIGH_IMPACT_TOPICS: Set[str] = {
    "probe", "spy", "scandal", "lawsuit", "fraud", "sec", "doj", "strike",
    "tariff", "subpoena", "breach", "antitrust", "recall", "earning", "guidance",
    "merger", "acquisition", "buyout", "bankruptcy", "delisting",
    "실적", "어닝", "수주", "유상증자", "횡령", "배임", "거래정지", "압수수색", "리콜", "관세", "인수",
}


def build_cluster_alias_key(cluster_id: str) -> str:
    return f"cluster:{cluster_id.strip()}"


def build_url_alias_key(url: str) -> str:
    digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:24]
    return f"url:{digest}"


def _normalize_en_token(word: str) -> str:
    w = _SYNONYMS.get(word, word)
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    elif w.endswith("es") and len(w) > 4:
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        w = w[:-1]
    return _SYNONYMS.get(w, w)


def extract_normalized_tokens(title: str) -> Set[str]:
    """한국어/영문 뉴스 제목에서 정규화된 핵심 토큰 집합을 추출한다."""
    raw_tokens = re.findall(r"[A-Za-z0-9]{2,}|[가-힣]{2,}", title.lower())
    tokens: Set[str] = set()
    for raw in raw_tokens:
        if raw in _STOPWORDS:
            continue
        if re.fullmatch(r"[a-z]+", raw):
            norm = _normalize_en_token(raw)
        else:
            norm = _SYNONYMS.get(raw, raw)
        if norm not in _STOPWORDS and len(norm) >= 2:
            tokens.add(norm)
    return tokens


def compute_token_similarity(tokens_a: Set[str], tokens_b: Set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    inter = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return inter / union if union > 0 else 0.0


def is_same_event(
    title_a: str,
    tokens_a: Set[str],
    title_b: str,
    tokens_b: Set[str],
    similarity_threshold: float = 0.35,
) -> bool:
    """두 기사가 언론사/표현/시간만 다른 동일 사건인지 판정한다."""
    if not tokens_a or not tokens_b:
        return False

    a_low = title_a.strip().lower()
    b_low = title_b.strip().lower()
    if a_low == b_low or (len(a_low) >= 12 and a_low in b_low) or (len(b_low) >= 12 and b_low in a_low):
        return True

    sim = compute_token_similarity(tokens_a, tokens_b)
    if sim >= similarity_threshold:
        return True

    inter = tokens_a & tokens_b
    # 핵심 사건 토픽 단어를 포함해 3개 이상 핵심 토큰이 겹치면 동일 사건으로 병합
    if len(inter) >= 3 and bool(inter & _HIGH_IMPACT_TOPICS):
        return True

    # 짧은 제목(4~6토큰)에서 겹치는 비율(Overlap Coefficient)이 매우 높은 경우
    min_len = min(len(tokens_a), len(tokens_b))
    if min_len >= 3 and (len(inter) / min_len) >= 0.70:
        return True

    return False


@dataclass(frozen=True)
class EventNodeMatch:
    matched: bool
    match_type: str  # "exact_alias" | "similar_node" | "none"
    node: Optional[Dict[str, Any]] = None


class EventDeduper:
    """공유 인텔리전스 DB 기반 사건 노드 중복 제거 및 병합기."""

    def __init__(
        self,
        *,
        similarity_threshold: float = 0.35,
        lookback_hours: float = 48.0,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.lookback_hours = lookback_hours

    async def resolve_or_bind(
        self,
        conn: aiosqlite.Connection,
        *,
        alias_key: str,
        symbol: str,
        market: str,
        title: str,
        source: Optional[str] = None,
        published_at: Optional[float] = None,
    ) -> EventNodeMatch:
        """1차 alias 정확 조회 → 2차 유사 사건 노드 탐색 및 Alias 병합."""
        # 1. 정확 조회 (cluster_id 또는 url 해시)
        existing = await intel_db.get_node_by_alias(conn, alias_key)
        if existing is not None:
            return EventNodeMatch(matched=True, match_type="exact_alias", node=existing)

        # 2. 동일 종목 최근 사건 노드와 유사도 비교
        cand_tokens = extract_normalized_tokens(title)
        recent_nodes = await intel_db.list_recent_nodes_for_symbol(
            conn,
            symbol,
            market=market,
            lookback_hours=self.lookback_hours,
        )
        for node in recent_nodes:
            try:
                node_tokens = set(json.loads(node.get("tokens_json") or "[]"))
            except Exception:
                node_tokens = extract_normalized_tokens(node.get("canonical_title") or "")

            if is_same_event(
                title,
                cand_tokens,
                node.get("canonical_title") or "",
                node_tokens,
                similarity_threshold=self.similarity_threshold,
            ):
                # 동일 사건 노드에 새 alias를 묶어(bind) 이후 조회는 O(1) 정확 매칭으로 처리
                await intel_db.bind_alias_to_node(
                    conn,
                    alias_key=alias_key,
                    node_id=node["node_id"],
                    symbol=symbol,
                    source=source,
                    title=title,
                    published_at=published_at,
                )
                return EventNodeMatch(matched=True, match_type="similar_node", node=node)

        return EventNodeMatch(matched=False, match_type="none", node=None)

    async def register_node(
        self,
        conn: aiosqlite.Connection,
        *,
        alias_key: str,
        symbol: str,
        market: str,
        title: str,
        source: Optional[str] = None,
        published_at: Optional[float] = None,
        node_id: Optional[str] = None,
        surprise_score: Optional[int] = None,
        sentiment_direction: Optional[str] = None,
        sentiment_score: Optional[float] = None,
        sentiment_reasoning: Optional[str] = None,
        impact_type: Optional[str] = None,
        affected_sector: Optional[str] = None,
        summary_ko: Optional[str] = None,
        alerted: bool = False,
        model_used: Optional[str] = None,
        raw_data: Optional[Dict[str, Any]] = None,
        extra_alias_keys: Optional[List[str]] = None,
    ) -> str:
        """신규 사건 노드를 등록하고 기본/추가 Alias를 모두 바인딩한다."""
        tokens = sorted(extract_normalized_tokens(title))
        if not node_id:
            seed = f"{market}:{symbol.upper()}:{alias_key}:{time.time()}"
            node_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]

        await intel_db.insert_event_node(
            conn,
            node_id=node_id,
            symbol=symbol,
            market=market,
            canonical_title=title,
            tokens=tokens,
            surprise_score=surprise_score,
            sentiment_direction=sentiment_direction,
            sentiment_score=sentiment_score,
            sentiment_reasoning=sentiment_reasoning,
            impact_type=impact_type,
            affected_sector=affected_sector,
            summary_ko=summary_ko,
            alerted=alerted,
            model_used=model_used,
            raw_data=raw_data,
        )

        all_aliases = [alias_key] + list(extra_alias_keys or [])
        for ak in dict.fromkeys(all_aliases):
            await intel_db.bind_alias_to_node(
                conn,
                alias_key=ak,
                node_id=node_id,
                symbol=symbol,
                source=source,
                title=title,
                published_at=published_at,
            )
        return node_id
