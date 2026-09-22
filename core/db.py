# core/db.py
"""SQLite 기반 영속성 계층.

Redis 대신 SQLite를 쓰는 이유: 개인 단일 프로세스 시스템에서는 별도로 떠 있어야 하는
서버(자체가 장애점이 됨) 없이 파일 기반 ACID를 보장하는 쪽이 더 견고하다. 크래시 후
재기동 시 order_intents/positions 상태를 신뢰할 수 있어야 하므로, 모든 쓰기는 커밋을
명시적으로 기다린다 (버퍼링된 채로 죽으면 reconciliation의 전제가 깨진다).
"""
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from core.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_symbols (
    symbol TEXT PRIMARY KEY,
    market TEXT NOT NULL DEFAULT 'domestic',   -- 'domestic' | 'overseas'
    exchange TEXT,                              -- overseas일 때만: NASD/NYSE/AMEX 등 (OVRS_EXCG_CD)
    added_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

-- append-only: 모든 제안/승인/거부 이력을 영구 보존 (audit trail)
CREATE TABLE IF NOT EXISTS target_weights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    weight REAL NOT NULL,
    status TEXT NOT NULL,       -- PROPOSED | APPROVED | REJECTED
    proposed_by TEXT NOT NULL,  -- 'manual' | 'llm'
    rationale TEXT,
    proposed_at REAL NOT NULL,
    decided_at REAL
);

-- 파생(materialized) 테이블: 엔진은 오직 이 테이블만 읽는다. 승인 안 된 종목은 여기 없음
-- -> "미승인 종목은 절대 매매하지 않는다"가 단순 조회 하나로 보장된다.
CREATE TABLE IF NOT EXISTS active_target_weights (
    symbol TEXT PRIMARY KEY,
    weight REAL NOT NULL,
    approved_at REAL NOT NULL,
    source_proposal_id INTEGER NOT NULL
);

-- KIS 잔고조회 결과로 주기적으로 강제 재동기화되는 ground truth 포지션
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'KRW',
    last_synced_at REAL NOT NULL
);

-- 엔진의 루프 1회 통과를 식별 (order_intents의 (symbol, cycle_id) UNIQUE 제약의 기준)
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    cycle_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'domestic',
    side TEXT NOT NULL,             -- buy | sell
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,       -- limit | market
    price REAL,
    status TEXT NOT NULL,           -- PENDING | SUBMITTED | FILLED | PARTIALLY_FILLED | REJECTED | CANCELLED | NOT_SUBMITTED | UNKNOWN
    kis_order_no TEXT,
    client_match_key TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (symbol, cycle_id)
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
    qty REAL NOT NULL,
    price REAL NOT NULL,
    filled_at REAL NOT NULL,
    source TEXT NOT NULL            -- WS | REST_POLL
);

-- 싱글턴 key-value: run flag, kill-switch, heartbeat, 락 정보, 일일손실 누적 등
CREATE TABLE IF NOT EXISTS engine_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    ts REAL NOT NULL,
    current_weight REAL,
    target_weight REAL,
    drift REAL,
    tech_signal TEXT,
    sentiment_signal TEXT,
    action TEXT NOT NULL,           -- NO_OP | BUY | SELL
    qty REAL,
    reason TEXT,
    intent_id TEXT REFERENCES order_intents(intent_id),
    context_json TEXT               -- 원시 입력 스냅샷(JSON, 재현/디버깅용) - 사후 복구 불가하므로 항상 채울 것
);

-- 30분봉 캐시(국내는 1분봉을 리샘플링, 해외는 KIS가 30분봉을 직접 반환) — 매 사이클
-- KIS를 다시 때리지 않도록 재사용한다.
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,        -- 'domestic' | 'overseas'
    bar_start REAL NOT NULL,     -- epoch seconds, 30분 버킷 시작 시각 (KST 09:00 앵커)
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, market, bar_start)
);

-- 펀더멘털 밸류에이션 입력값 캐시. 분기 단위로만 바뀌는 데이터라 사이클마다 재조회하지 않는다.
CREATE TABLE IF NOT EXISTS fundamentals_cache (
    symbol TEXT PRIMARY KEY,
    per REAL, pbr REAL, per_percentile REAL, pbr_percentile REAL,
    target_price_mean REAL, target_gap_pct REAL, recomm_mean REAL,
    w52_position_pct REAL,
    roe REAL, debt_ratio REAL,
    raw_json TEXT,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS news_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    published_at REAL,
    raw_text TEXT,
    fetched_at REAL NOT NULL,
    sentiment_score REAL,
    sentiment_reasoning TEXT
);

-- AI 포트폴리오 에이전트: 리밸런싱 제안 1건 = 비중변경(+추후 종목추가/제외)을 묶은 이벤트.
-- target_weights 행들이 이 이벤트를 rebalance_event_id로 역참조해 어느 제안에서 나왔는지 추적한다.
CREATE TABLE IF NOT EXISTS rebalance_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,        -- 'MANUAL' | 'SCHEDULED' | 'DRIFT' | 'NEWS_EVENT' | 'ROLLBACK'
    trigger_detail TEXT,
    autonomy_mode TEXT NOT NULL,       -- 'approval_gated' | 'full_auto' (실행 시점 스냅샷)
    status TEXT NOT NULL,              -- RUNNING | PROPOSED | APPROVED | REJECTED | AUTO_APPLIED | BLOCKED | FAILED | SKIPPED
    rationale TEXT,
    symbols_added TEXT,                -- JSON [{symbol, name, rationale}]
    symbols_removed TEXT,              -- JSON [{symbol, rationale}]
    prior_snapshot_json TEXT NOT NULL, -- 실행 직전 {symbol: weight} + 종목 세트
    proposed_snapshot_json TEXT,       -- 제안된 사후 상태 (검증 실패 시 NULL)
    context_json TEXT,                 -- LLM 입출력 전체 스냅샷 (decision_log와 동일하게 항상 채울 것)
    error TEXT,
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT                    -- 'human' | 'circuit_breaker' | 'auto'
);

-- 종목 발굴 후보 풀. LLM은 이 안에서만 신규 편입을 제안할 수 있다 (환각 티커 방지) —
-- 여기 들어오는 시점에 이미 KIS 실재성 검증을 통과한 것만 담긴다.
CREATE TABLE IF NOT EXISTS candidate_universe (
    symbol TEXT PRIMARY KEY,
    market TEXT NOT NULL DEFAULT 'domestic',
    exchange TEXT,
    name TEXT,
    universe_tag TEXT NOT NULL,        -- 'KOSPI_LARGE_CAP' | 'MANUAL_WATCHLIST' 등
    validated_at REAL,
    added_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

-- 종목별 편입 논거. 나중에 "왜 이 종목을 뺐는지" 판단할 근거로 재사용한다.
CREATE TABLE IF NOT EXISTS symbol_thesis (
    symbol TEXT PRIMARY KEY,
    added_reason TEXT,
    added_by TEXT NOT NULL,            -- 'manual' | 'llm'
    thesis_json TEXT,
    source_event_id INTEGER,           -- rebalance_events.id, 수동 추가는 NULL
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


async def get_connection() -> aiosqlite.Connection:
    Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(settings.DB_PATH)
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = aiosqlite.Row
    return conn


async def init_db() -> None:
    conn = await get_connection()
    try:
        await conn.executescript(SCHEMA)
        await _migrate_add_missing_columns(conn)
        await conn.commit()
    finally:
        await conn.close()


async def _migrate_add_missing_columns(conn: aiosqlite.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS는 이미 존재하는 테이블의 컬럼을 추가해주지 않으므로,
    기존 DB 파일에 새로 추가된 컬럼을 놓치지 않도록 가벼운 마이그레이션을 직접 처리한다."""
    cur = await conn.execute("PRAGMA table_info(decision_log)")
    columns = {row["name"] for row in await cur.fetchall()}
    if "context_json" not in columns:
        await conn.execute("ALTER TABLE decision_log ADD COLUMN context_json TEXT")

    cur = await conn.execute("PRAGMA table_info(portfolio_symbols)")
    columns = {row["name"] for row in await cur.fetchall()}
    if "exchange" not in columns:
        await conn.execute("ALTER TABLE portfolio_symbols ADD COLUMN exchange TEXT")

    cur = await conn.execute("PRAGMA table_info(positions)")
    columns = {row["name"] for row in await cur.fetchall()}
    if "entry_avg_price" not in columns:
        await conn.execute("ALTER TABLE positions ADD COLUMN entry_avg_price REAL")
    if "peak_price_since_entry" not in columns:
        await conn.execute("ALTER TABLE positions ADD COLUMN peak_price_since_entry REAL")
    if "entry_opened_at" not in columns:
        await conn.execute("ALTER TABLE positions ADD COLUMN entry_opened_at REAL")

    cur = await conn.execute("PRAGMA table_info(target_weights)")
    columns = {row["name"] for row in await cur.fetchall()}
    if "rebalance_event_id" not in columns:
        await conn.execute("ALTER TABLE target_weights ADD COLUMN rebalance_event_id INTEGER")

    cur = await conn.execute("PRAGMA table_info(portfolio_symbols)")
    columns = {row["name"] for row in await cur.fetchall()}
    if "disabled_at" not in columns:
        await conn.execute("ALTER TABLE portfolio_symbols ADD COLUMN disabled_at REAL")
    if "disabled_by_event_id" not in columns:
        await conn.execute("ALTER TABLE portfolio_symbols ADD COLUMN disabled_by_event_id INTEGER")


# --- engine_state (singleton key-value) ---

async def set_state(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO engine_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await conn.commit()


async def get_state(conn: aiosqlite.Connection, key: str) -> Optional[str]:
    cur = await conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else None


# --- portfolio_symbols ---

async def add_portfolio_symbol(
    conn: aiosqlite.Connection, symbol: str, market: str = "domestic", exchange: Optional[str] = None
) -> None:
    await conn.execute(
        "INSERT INTO portfolio_symbols(symbol, market, exchange, added_at, enabled) VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(symbol) DO UPDATE SET enabled = 1, market = excluded.market, exchange = excluded.exchange",
        (symbol, market, exchange, time.time()),
    )
    await conn.commit()


async def list_portfolio_symbols(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM portfolio_symbols WHERE enabled = 1")
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_portfolio_symbol_info(conn: aiosqlite.Connection) -> Dict[str, Dict[str, Any]]:
    """symbol -> {"market": ..., "exchange": ...}. 사이클에서 국내/해외 처리 경로를 나누는 데 사용."""
    rows = await list_portfolio_symbols(conn)
    return {row["symbol"]: {"market": row["market"], "exchange": row["exchange"]} for row in rows}


async def get_recent_decisions(conn: aiosqlite.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


# --- intraday_bars ---

async def get_cached_intraday_bars(
    conn: aiosqlite.Connection, symbol: str, market: str, since_ts: float
) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM intraday_bars WHERE symbol = ? AND market = ? AND bar_start >= ? "
        "ORDER BY bar_start ASC",
        (symbol, market, since_ts),
    )
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def save_intraday_bars(
    conn: aiosqlite.Connection, symbol: str, market: str, bars: List[Dict[str, Any]]
) -> None:
    for bar in bars:
        await conn.execute(
            "INSERT INTO intraday_bars(symbol, market, bar_start, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, market, bar_start) DO UPDATE SET open=excluded.open, "
            "high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume",
            (symbol, market, bar["bar_start"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]),
        )
    await conn.commit()


# --- fundamentals_cache ---

async def get_cached_valuation(
    conn: aiosqlite.Connection, symbol: str, max_age_hours: float
) -> Optional[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM fundamentals_cache WHERE symbol = ?", (symbol,))
    row = await cur.fetchone()
    if row is None:
        return None
    if (time.time() - row["fetched_at"]) > max_age_hours * 3600:
        return None
    return dict(row)


async def cache_valuation(conn: aiosqlite.Connection, symbol: str, **fields: Any) -> None:
    cols = list(fields.keys()) + ["fetched_at"]
    values = list(fields.values()) + [time.time()]
    placeholders = ", ".join("?" for _ in cols)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    await conn.execute(
        f"INSERT INTO fundamentals_cache(symbol, {', '.join(cols)}) VALUES (?, {placeholders}) "
        f"ON CONFLICT(symbol) DO UPDATE SET {update_clause}",
        (symbol, *values),
    )
    await conn.commit()


# --- news_cache ---

async def get_cached_news_sentiment(conn: aiosqlite.Connection, url: str) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT sentiment_score, sentiment_reasoning FROM news_cache WHERE url = ? "
        "AND sentiment_score IS NOT NULL",
        (url,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def cache_news_sentiment(
    conn: aiosqlite.Connection, symbol: str, source: str, url: str, published_at: Optional[float],
    raw_text: str, sentiment_score: float, sentiment_reasoning: str,
) -> None:
    await conn.execute(
        "INSERT INTO news_cache(symbol, source, url, published_at, raw_text, fetched_at, "
        "sentiment_score, sentiment_reasoning) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET sentiment_score=excluded.sentiment_score, "
        "sentiment_reasoning=excluded.sentiment_reasoning",
        (symbol, source, url, published_at, raw_text, time.time(), sentiment_score, sentiment_reasoning),
    )
    await conn.commit()


# --- active_target_weights (엔진의 유일한 읽기 경로) ---

async def get_active_target_weights(conn: aiosqlite.Connection) -> Dict[str, float]:
    cur = await conn.execute("SELECT symbol, weight FROM active_target_weights")
    rows = await cur.fetchall()
    return {row["symbol"]: row["weight"] for row in rows}


async def propose_target_weight(
    conn: aiosqlite.Connection,
    symbol: str,
    weight: float,
    proposed_by: str,
    rationale: str = "",
    rebalance_event_id: Optional[int] = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO target_weights(symbol, weight, status, proposed_by, rationale, proposed_at, "
        "rebalance_event_id) VALUES (?, ?, 'PROPOSED', ?, ?, ?, ?)",
        (symbol, weight, proposed_by, rationale, time.time(), rebalance_event_id),
    )
    await conn.commit()
    return cur.lastrowid


async def get_pending_weight_proposals(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM target_weights WHERE status = 'PROPOSED' ORDER BY proposed_at DESC"
    )
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def decide_target_weight(conn: aiosqlite.Connection, proposal_id: int, approve: bool) -> None:
    now = time.time()
    await conn.execute(
        "UPDATE target_weights SET status = ?, decided_at = ? WHERE id = ?",
        ("APPROVED" if approve else "REJECTED", now, proposal_id),
    )
    if approve:
        cur = await conn.execute(
            "SELECT symbol, weight FROM target_weights WHERE id = ?", (proposal_id,)
        )
        row = await cur.fetchone()
        await conn.execute(
            "INSERT INTO active_target_weights(symbol, weight, approved_at, source_proposal_id) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET weight=excluded.weight, "
            "approved_at=excluded.approved_at, source_proposal_id=excluded.source_proposal_id",
            (row["symbol"], row["weight"], now, proposal_id),
        )
    await conn.commit()


# --- rebalance_events (AI 포트폴리오 에이전트 감사로그) ---

async def create_rebalance_event(
    conn: aiosqlite.Connection,
    trigger_type: str,
    autonomy_mode: str,
    prior_snapshot_json: str,
    trigger_detail: str = "",
) -> int:
    cur = await conn.execute(
        "INSERT INTO rebalance_events(trigger_type, trigger_detail, autonomy_mode, status, "
        "prior_snapshot_json, created_at) VALUES (?, ?, ?, 'RUNNING', ?, ?)",
        (trigger_type, trigger_detail, autonomy_mode, prior_snapshot_json, time.time()),
    )
    await conn.commit()
    return cur.lastrowid


async def finish_rebalance_event(
    conn: aiosqlite.Connection,
    event_id: int,
    status: str,
    proposed_snapshot_json: Optional[str] = None,
    rationale: Optional[str] = None,
    context_json: Optional[str] = None,
    error: Optional[str] = None,
    symbols_added_json: Optional[str] = None,
    symbols_removed_json: Optional[str] = None,
) -> None:
    await conn.execute(
        "UPDATE rebalance_events SET status = ?, proposed_snapshot_json = COALESCE(?, proposed_snapshot_json), "
        "rationale = COALESCE(?, rationale), context_json = COALESCE(?, context_json), "
        "error = COALESCE(?, error), symbols_added = COALESCE(?, symbols_added), "
        "symbols_removed = COALESCE(?, symbols_removed) WHERE id = ?",
        (status, proposed_snapshot_json, rationale, context_json, error,
         symbols_added_json, symbols_removed_json, event_id),
    )
    await conn.commit()


async def get_rebalance_event(conn: aiosqlite.Connection, event_id: int) -> Optional[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM rebalance_events WHERE id = ?", (event_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_rebalance_events(conn: aiosqlite.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM rebalance_events ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def apply_rebalance_event(
    conn: aiosqlite.Connection, event_id: int, approve: bool, decided_by: str
) -> None:
    """이벤트에 묶인 모든 target_weights 행을 한 트랜잭션 개념으로 일괄 승인/거부한다.

    승인 시에만 symbols_added를 portfolio_symbols(+thesis)에 반영한다 - 사람이 거부하면
    아무것도 실제 포트폴리오에 닿지 않는다. symbols_removed는 별도 처리가 필요 없다 —
    "제외"는 항상 target_weight=0 제안으로 표현되고(core/portfolio_agent.py), 그 비중이
    decide_target_weight를 통해 active_target_weights에 그대로 반영되면 기존
    signal_engine의 ceiling 로직이 초과분을 밴드 경계까지 강제 축소한다. portfolio_symbols
    자체를 여기서 비활성화하지 않는 이유: 해외종목의 market/exchange 메타데이터가
    get_portfolio_symbol_info()에서 사라지면 엔진이 국내로 잘못 취급할 위험이 있다 —
    잔여 포지션이 있는 한 계속 정확한 메타데이터로 관리되어야 한다.
    """
    event = await get_rebalance_event(conn, event_id)
    if event is None:
        raise ValueError(f"rebalance_event {event_id}를 찾을 수 없음")

    cur = await conn.execute(
        "SELECT id FROM target_weights WHERE rebalance_event_id = ? AND status = 'PROPOSED'", (event_id,)
    )
    weight_ids = [row["id"] for row in await cur.fetchall()]

    if approve and event["symbols_added"]:
        for add in json.loads(event["symbols_added"]):
            await add_portfolio_symbol(conn, add["symbol"], market="domestic")
            await upsert_symbol_thesis(
                conn, add["symbol"], added_reason=add.get("rationale", ""), added_by="llm",
                source_event_id=event_id,
            )

    for wid in weight_ids:
        await decide_target_weight(conn, wid, approve)

    now = time.time()
    await conn.execute(
        "UPDATE rebalance_events SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
        ("APPROVED" if approve else "REJECTED", now, decided_by, event_id),
    )
    await conn.commit()


# --- candidate_universe / symbol_thesis (AI 포트폴리오 에이전트 종목 발굴) ---

async def add_candidate_symbol(
    conn: aiosqlite.Connection, symbol: str, name: str, universe_tag: str,
    market: str = "domestic", exchange: Optional[str] = None,
) -> None:
    now = time.time()
    await conn.execute(
        "INSERT INTO candidate_universe(symbol, market, exchange, name, universe_tag, validated_at, "
        "added_at, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, validated_at=excluded.validated_at, enabled=1",
        (symbol, market, exchange, name, universe_tag, now, now),
    )
    await conn.commit()


async def list_candidate_universe(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM candidate_universe WHERE enabled = 1")
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def upsert_symbol_thesis(
    conn: aiosqlite.Connection, symbol: str, added_reason: str, added_by: str,
    thesis_json: Optional[str] = None, source_event_id: Optional[int] = None,
) -> None:
    now = time.time()
    await conn.execute(
        "INSERT INTO symbol_thesis(symbol, added_reason, added_by, thesis_json, source_event_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET added_reason=excluded.added_reason, added_by=excluded.added_by, "
        "thesis_json=excluded.thesis_json, source_event_id=excluded.source_event_id, updated_at=excluded.updated_at",
        (symbol, added_reason, added_by, thesis_json, source_event_id, now, now),
    )
    await conn.commit()


async def get_symbol_thesis(conn: aiosqlite.Connection, symbol: str) -> Optional[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM symbol_thesis WHERE symbol = ?", (symbol,))
    row = await cur.fetchone()
    return dict(row) if row else None


# --- positions ---

async def upsert_position(
    conn: aiosqlite.Connection, symbol: str, qty: float, avg_price: float, currency: str = "KRW"
) -> None:
    """KIS 잔고조회로 포지션을 덮어쓰는 유일한 지점. 트레일링 익절의 "진입 후 고점" 상태를
    여기서 관리한다 — 0->양수 전환은 새 진입(초기화), 양수->0 전환은 완전청산(초기화), 그
    사이(추가매수/부분매도)는 절대 건드리지 않는다(추가매수 한다고 고점이 리셋되면 트레일링
    보호가 무의미해진다)."""
    cur = await conn.execute("SELECT qty FROM positions WHERE symbol = ?", (symbol,))
    row = await cur.fetchone()
    prev_qty = float(row["qty"]) if row else 0.0

    if prev_qty <= 0 and qty > 0:
        entry_avg_price, peak_price, entry_opened_at = avg_price, avg_price, time.time()
    elif qty <= 0:
        entry_avg_price = peak_price = entry_opened_at = None
    else:
        cur = await conn.execute(
            "SELECT entry_avg_price, peak_price_since_entry, entry_opened_at FROM positions WHERE symbol = ?",
            (symbol,),
        )
        existing = await cur.fetchone()
        entry_avg_price = existing["entry_avg_price"] if existing else avg_price
        peak_price = existing["peak_price_since_entry"] if existing else avg_price
        entry_opened_at = existing["entry_opened_at"] if existing else time.time()

    await conn.execute(
        "INSERT INTO positions(symbol, qty, avg_price, currency, last_synced_at, "
        "entry_avg_price, peak_price_since_entry, entry_opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price, "
        "currency=excluded.currency, last_synced_at=excluded.last_synced_at, "
        "entry_avg_price=excluded.entry_avg_price, peak_price_since_entry=excluded.peak_price_since_entry, "
        "entry_opened_at=excluded.entry_opened_at",
        (symbol, qty, avg_price, currency, time.time(), entry_avg_price, peak_price, entry_opened_at),
    )
    await conn.commit()


async def update_position_peak(conn: aiosqlite.Connection, symbol: str, current_price: float) -> None:
    """실시간가는 잔고 동기화 시점이 아니라 종목별 가격조회 시점에만 있으므로 별도 호출."""
    await conn.execute(
        "UPDATE positions SET peak_price_since_entry = MAX(COALESCE(peak_price_since_entry, 0), ?) "
        "WHERE symbol = ? AND qty > 0",
        (current_price, symbol),
    )
    await conn.commit()


async def get_position(conn: aiosqlite.Connection, symbol: str) -> Optional[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_positions(conn: aiosqlite.Connection) -> Dict[str, Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM positions")
    rows = await cur.fetchall()
    return {row["symbol"]: dict(row) for row in rows}


# --- cycles ---

async def new_cycle(conn: aiosqlite.Connection) -> int:
    cur = await conn.execute("INSERT INTO cycles(started_at) VALUES (?)", (time.time(),))
    await conn.commit()
    return cur.lastrowid


# --- order_intents ---

def make_client_match_key(symbol: str, side: str, qty: float, price: Optional[float]) -> str:
    price_part = f"{price:.2f}" if price is not None else "MKT"
    return f"{symbol}:{side}:{qty}:{price_part}"


async def create_order_intent(
    conn: aiosqlite.Connection,
    cycle_id: int,
    symbol: str,
    side: str,
    qty: float,
    order_type: str,
    price: Optional[float],
    reason: str,
    market: str = "domestic",
) -> str:
    """주문 요청을 KIS에 보내기 *전에* 반드시 호출해 커밋까지 완료해야 한다.

    (symbol, cycle_id) UNIQUE 제약이 이중주문 방지의 최후 방어선이므로, 이 INSERT가
    실패(IntegrityError)하면 이미 이번 사이클에 이 종목에 대한 intent가 있다는 뜻 —
    호출자는 그 예외를 잡아 주문을 보내지 말아야 한다.
    """
    intent_id = str(uuid.uuid4())
    now = time.time()
    client_match_key = make_client_match_key(symbol, side, qty, price)
    await conn.execute(
        "INSERT INTO order_intents(intent_id, cycle_id, symbol, market, side, qty, order_type, "
        "price, status, kis_order_no, client_match_key, reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, ?, ?, ?, ?)",
        (intent_id, cycle_id, symbol, market, side, qty, order_type, price,
         client_match_key, reason, now, now),
    )
    await conn.commit()
    return intent_id


async def update_order_intent(
    conn: aiosqlite.Connection,
    intent_id: str,
    status: str,
    kis_order_no: Optional[str] = None,
) -> None:
    await conn.execute(
        "UPDATE order_intents SET status = ?, kis_order_no = COALESCE(?, kis_order_no), "
        "updated_at = ? WHERE intent_id = ?",
        (status, kis_order_no, time.time(), intent_id),
    )
    await conn.commit()


async def get_unresolved_intents(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """재기동 시 reconciliation 대상: 아직 최종 상태가 아닌 intent들."""
    cur = await conn.execute(
        "SELECT * FROM order_intents WHERE status IN ('PENDING', 'SUBMITTED')"
    )
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


# --- decision_log ---

async def log_decision(
    conn: aiosqlite.Connection,
    cycle_id: int,
    symbol: str,
    current_weight: Optional[float],
    target_weight: Optional[float],
    drift: Optional[float],
    tech_signal: str,
    sentiment_signal: str,
    action: str,
    qty: Optional[float],
    reason: str,
    intent_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """context: 재현/디버깅용 원시 스냅샷(원시 tech/sentiment 시그널, 고려한 기사, 가격,
    클램프 전/후 수량 등). 사후에 복구할 수 없는 정보이므로 호출부는 항상 넘겨줄 것."""
    await conn.execute(
        "INSERT INTO decision_log(cycle_id, symbol, ts, current_weight, target_weight, drift, "
        "tech_signal, sentiment_signal, action, qty, reason, intent_id, context_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cycle_id, symbol, time.time(), current_weight, target_weight, drift,
         tech_signal, sentiment_signal, action, qty, reason, intent_id,
         json.dumps(context, ensure_ascii=False, default=str) if context is not None else None),
    )
    await conn.commit()
