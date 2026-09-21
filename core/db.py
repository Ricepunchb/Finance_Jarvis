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
    market TEXT NOT NULL DEFAULT 'domestic',   -- 'domestic' | 'overseas' (Phase 3에서 사용)
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

async def add_portfolio_symbol(conn: aiosqlite.Connection, symbol: str, market: str = "domestic") -> None:
    await conn.execute(
        "INSERT INTO portfolio_symbols(symbol, market, added_at, enabled) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(symbol) DO UPDATE SET enabled = 1",
        (symbol, market, time.time()),
    )
    await conn.commit()


async def list_portfolio_symbols(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM portfolio_symbols WHERE enabled = 1")
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_recent_decisions(conn: aiosqlite.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


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
    conn: aiosqlite.Connection, symbol: str, weight: float, proposed_by: str, rationale: str = ""
) -> int:
    cur = await conn.execute(
        "INSERT INTO target_weights(symbol, weight, status, proposed_by, rationale, proposed_at) "
        "VALUES (?, ?, 'PROPOSED', ?, ?, ?)",
        (symbol, weight, proposed_by, rationale, time.time()),
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


# --- positions ---

async def upsert_position(
    conn: aiosqlite.Connection, symbol: str, qty: float, avg_price: float, currency: str = "KRW"
) -> None:
    await conn.execute(
        "INSERT INTO positions(symbol, qty, avg_price, currency, last_synced_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price, "
        "currency=excluded.currency, last_synced_at=excluded.last_synced_at",
        (symbol, qty, avg_price, currency, time.time()),
    )
    await conn.commit()


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
