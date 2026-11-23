# core/intel/db.py
"""공유 인텔리전스 DB (`data/jarvis_intel.db`) 스키마 및 Repository.

계좌별 트레이딩 DB(`data/jarvis_{profile}.db`)와 의도적으로 분리하여,
여러 프로필(real, isa, mock, meritz)이 동일한 뉴스/리포트/인사이트를
중복 분석하지 않고 단일 마스터 DB를 안전하게 공유(SQLite WAL)하도록 한다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from core.config import settings

INTEL_SCHEMA_SQL = """
-- 1. 뉴스 사건 노드 (1개 사건 = 1행; 75점 미만 포함 전량 캐싱)
CREATE TABLE IF NOT EXISTS news_event_nodes (
    node_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'US',          -- 'US' | 'KR'
    canonical_title TEXT NOT NULL,
    tokens_json TEXT NOT NULL,                  -- 정규화 핵심 토큰 JSON 리스트
    surprise_score INTEGER,                     -- 0~100 (미장 서프라이즈)
    sentiment_direction TEXT,                   -- 'BUY' | 'SELL' | 'HOLD'
    sentiment_score REAL,                       -- -1.0 ~ +1.0
    sentiment_reasoning TEXT,
    impact_type TEXT,                           -- '호재' | '악재' | '중립'
    affected_sector TEXT,
    summary_ko TEXT,
    alerted INTEGER NOT NULL DEFAULT 0,         -- 텔레그램 발송 여부 (0/1)
    model_used TEXT,
    raw_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intel_nodes_sym_created
    ON news_event_nodes(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intel_nodes_market_created
    ON news_event_nodes(market, created_at DESC);

-- 2. 클러스터/기사 Alias -> 사건 노드 매핑 (다수 언론사/시간대 기사 통합)
CREATE TABLE IF NOT EXISTS news_cluster_aliases (
    alias_key TEXT PRIMARY KEY,                 -- 'cluster:<id>' 또는 'url:<sha256>'
    node_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT,
    title TEXT,
    published_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY(node_id) REFERENCES news_event_nodes(node_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_intel_aliases_node ON news_cluster_aliases(node_id);
CREATE INDEX IF NOT EXISTS idx_intel_aliases_sym_pub
    ON news_cluster_aliases(symbol, published_at DESC);

-- 3. 공유 증권사 리포트 분석 캐시 (계좌 간 3중 중복 분석 제거)
CREATE TABLE IF NOT EXISTS shared_research_reports (
    research_id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT,
    broker TEXT,
    title TEXT,
    write_date TEXT,
    opinion TEXT,
    target_price REAL,
    price_at_write REAL,
    content_text TEXT,
    analysis_json TEXT,
    analyzed_at REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shared_reports_sym ON shared_research_reports(symbol, write_date DESC);

-- 4. 공유 인사이트 다이제스트 스냅샷 (마스터 1회 생성 -> 전 계좌 참조)
CREATE TABLE IF NOT EXISTS shared_insight_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    digest_json TEXT,
    summary_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_shared_insight_started
    ON shared_insight_batches(started_at DESC);

-- 5. 일일/주간 LLM 토큰 사용량 원장
CREATE TABLE IF NOT EXISTS llm_usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_kst TEXT NOT NULL,                     -- 'YYYY-MM-DD'
    week_kst TEXT NOT NULL,                     -- 'YYYY-Www' (ISO 주차)
    timestamp REAL NOT NULL,
    profile TEXT NOT NULL,
    task_type TEXT NOT NULL,                    -- 'news_sentiment' | 'us_surprise_score' | 'us_surprise_summary' | 'report_batch' | 'insight_digest' | 'rebalance' | 'etf_classify'
    tier TEXT NOT NULL,                         -- 'decision' | 'fast' | 'smart'
    provider TEXT NOT NULL,                     -- 'gemini' | 'open_jev'
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    thought_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
    latency_ms REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'ok'           -- 'ok' | 'error' | 'budget_blocked' | 'shadow'
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_date ON llm_usage_ledger(date_kst);
CREATE INDEX IF NOT EXISTS idx_llm_usage_week ON llm_usage_ledger(week_kst);

-- 6. 마스터 데몬 상태/하트비트 KV
CREATE TABLE IF NOT EXISTS intel_daemon_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def resolve_intel_db_path(db_path: Optional[str] = None) -> Path:
    path = Path(db_path or getattr(settings, "INTEL_DB_PATH", "data/jarvis_intel.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def get_intel_connection(db_path: Optional[str] = None) -> aiosqlite.Connection:
    path = resolve_intel_db_path(db_path)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn


async def init_intel_db(db_path: Optional[str] = None) -> None:
    conn = await get_intel_connection(db_path)
    try:
        await conn.executescript(INTEL_SCHEMA_SQL)
        await conn.commit()
    finally:
        await conn.close()


# --- Daemon State KV ---


async def set_daemon_state(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        """
        INSERT INTO intel_daemon_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, time.time()),
    )
    await conn.commit()


async def get_daemon_state(conn: aiosqlite.Connection, key: str) -> Optional[str]:
    cur = await conn.execute("SELECT value FROM intel_daemon_state WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else None


# --- Event Nodes & Aliases ---


async def get_node_by_alias(
    conn: aiosqlite.Connection, alias_key: str
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        """
        SELECT n.*
        FROM news_cluster_aliases a
        JOIN news_event_nodes n ON n.node_id = a.node_id
        WHERE a.alias_key = ?
        LIMIT 1
        """,
        (alias_key,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_recent_nodes_for_symbol(
    conn: aiosqlite.Connection,
    symbol: str,
    *,
    market: Optional[str] = None,
    lookback_hours: float = 48.0,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    min_ts = time.time() - lookback_hours * 3600.0
    if market:
        cur = await conn.execute(
            """
            SELECT * FROM news_event_nodes
            WHERE symbol = ? AND market = ? AND created_at >= ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (symbol.strip().upper(), market.upper(), min_ts, limit),
        )
    else:
        cur = await conn.execute(
            """
            SELECT * FROM news_event_nodes
            WHERE symbol = ? AND created_at >= ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (symbol.strip().upper(), min_ts, limit),
        )
    return [dict(r) for r in await cur.fetchall()]


async def insert_event_node(
    conn: aiosqlite.Connection,
    *,
    node_id: str,
    symbol: str,
    market: str,
    canonical_title: str,
    tokens: List[str],
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
) -> None:
    now = time.time()
    await conn.execute(
        """
        INSERT INTO news_event_nodes (
            node_id, symbol, market, canonical_title, tokens_json,
            surprise_score, sentiment_direction, sentiment_score, sentiment_reasoning,
            impact_type, affected_sector, summary_ko, alerted, model_used, raw_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id) DO UPDATE SET
            surprise_score = COALESCE(excluded.surprise_score, news_event_nodes.surprise_score),
            sentiment_direction = COALESCE(excluded.sentiment_direction, news_event_nodes.sentiment_direction),
            sentiment_score = COALESCE(excluded.sentiment_score, news_event_nodes.sentiment_score),
            sentiment_reasoning = COALESCE(excluded.sentiment_reasoning, news_event_nodes.sentiment_reasoning),
            impact_type = COALESCE(excluded.impact_type, news_event_nodes.impact_type),
            affected_sector = COALESCE(excluded.affected_sector, news_event_nodes.affected_sector),
            summary_ko = COALESCE(excluded.summary_ko, news_event_nodes.summary_ko),
            alerted = MAX(news_event_nodes.alerted, excluded.alerted),
            model_used = COALESCE(excluded.model_used, news_event_nodes.model_used),
            raw_json = COALESCE(excluded.raw_json, news_event_nodes.raw_json),
            updated_at = excluded.updated_at
        """,
        (
            node_id,
            symbol.strip().upper(),
            market.upper(),
            canonical_title.strip(),
            json.dumps(sorted(set(tokens)), ensure_ascii=False),
            surprise_score,
            sentiment_direction,
            sentiment_score,
            sentiment_reasoning,
            impact_type,
            affected_sector,
            summary_ko,
            1 if alerted else 0,
            model_used,
            json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None,
            now,
            now,
        ),
    )
    await conn.commit()


async def bind_alias_to_node(
    conn: aiosqlite.Connection,
    *,
    alias_key: str,
    node_id: str,
    symbol: str,
    source: Optional[str] = None,
    title: Optional[str] = None,
    published_at: Optional[float] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO news_cluster_aliases (
            alias_key, node_id, symbol, source, title, published_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alias_key) DO NOTHING
        """,
        (
            alias_key,
            node_id,
            symbol.strip().upper(),
            source,
            title,
            published_at,
            time.time(),
        ),
    )
    await conn.commit()


# --- Shared Reports & Insight Batches ---


async def get_shared_report_analysis(
    conn: aiosqlite.Connection, research_id: int
) -> Optional[str]:
    cur = await conn.execute(
        "SELECT analysis_json FROM shared_research_reports WHERE research_id = ? AND analysis_json IS NOT NULL",
        (research_id,),
    )
    row = await cur.fetchone()
    return row["analysis_json"] if row else None


async def upsert_shared_report_analysis(
    conn: aiosqlite.Connection,
    *,
    research_id: int,
    symbol: str,
    name: str = "",
    broker: str = "",
    title: str = "",
    write_date: str = "",
    opinion: Optional[str] = None,
    target_price: Optional[float] = None,
    price_at_write: Optional[float] = None,
    content_text: Optional[str] = None,
    analysis_json: str,
) -> None:
    now = time.time()
    await conn.execute(
        """
        INSERT INTO shared_research_reports (
            research_id, symbol, name, broker, title, write_date,
            opinion, target_price, price_at_write, content_text,
            analysis_json, analyzed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(research_id) DO UPDATE SET
            analysis_json = excluded.analysis_json,
            analyzed_at = excluded.analyzed_at,
            updated_at = excluded.updated_at
        """,
        (
            research_id,
            symbol.strip().upper(),
            name,
            broker,
            title,
            write_date,
            opinion,
            target_price,
            price_at_write,
            content_text,
            analysis_json,
            now,
            now,
        ),
    )
    await conn.commit()


async def save_shared_insight_batch(
    conn: aiosqlite.Connection,
    *,
    trigger_type: str,
    status: str,
    digest: Optional[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> int:
    now = time.time()
    cur = await conn.execute(
        """
        INSERT INTO shared_insight_batches (
            trigger_type, status, started_at, finished_at, digest_json, summary_json, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trigger_type,
            status,
            now,
            now,
            json.dumps(digest, ensure_ascii=False) if digest else None,
            json.dumps(summary, ensure_ascii=False) if summary else None,
            error,
        ),
    )
    await conn.commit()
    return cur.lastrowid or 0


async def get_latest_shared_insight_digest(
    conn: aiosqlite.Connection, max_age_hours: float = 36.0
) -> Optional[Dict[str, Any]]:
    min_ts = time.time() - max_age_hours * 3600.0
    cur = await conn.execute(
        """
        SELECT id, digest_json, finished_at FROM shared_insight_batches
        WHERE status = 'DONE' AND digest_json IS NOT NULL AND finished_at >= ?
        ORDER BY finished_at DESC LIMIT 1
        """,
        (min_ts,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["digest_json"])
        if isinstance(data, dict):
            data["_shared_batch_id"] = row["id"]
            data["_finished_at"] = row["finished_at"]
            return data
    except Exception:
        return None
    return None
