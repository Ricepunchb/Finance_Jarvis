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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

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
    universe_tag TEXT NOT NULL,        -- 'KOSPI_LARGE_CAP' | 'MANUAL_WATCHLIST' | 'DYNAMIC' 등
    validated_at REAL,
    added_at REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    sources_json TEXT,                 -- 동적 후보: {source: evidence} (어느 발굴 소스가 왜 잡았는지)
    last_seen_at REAL,
    expires_at REAL                    -- NULL이면 만료 없음 (시드/수동 후보)
);

-- KIS 종목 마스터 파일(kospi/kosdaq_code.mst)의 필요한 컬럼만. 하루 1회 전체 교체.
CREATE TABLE IF NOT EXISTS stock_master (
    symbol TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL,              -- 'KOSPI' | 'KOSDAQ'
    sector_code TEXT,                  -- 지수업종 대분류
    market_cap_eok REAL,               -- 전일기준 시가총액(억원)
    roe REAL,
    is_excluded INTEGER NOT NULL,      -- 보통주 외/거래정지/정리매매/관리/시장경고/SPAC/ETP 중 하나라도 해당
    refreshed_at REAL NOT NULL
);

-- KIS 테마 마스터 파일(theme_code.mst). 종목 하나가 여러 테마에 속한다.
CREATE TABLE IF NOT EXISTS stock_theme (
    theme_code TEXT NOT NULL,
    theme_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    PRIMARY KEY (theme_code, symbol)
);
CREATE INDEX IF NOT EXISTS idx_stock_theme_symbol ON stock_theme(symbol);

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

-- 분석 대시보드용 일봉 종가 캐시. 과거 일자는 불변이라 한 번 받으면 다시 조회하지 않는다.
-- date는 거래소 현지 거래일(국내 KST, 해외 미국 현지일) 'YYYY-MM-DD'.
CREATE TABLE IF NOT EXISTS daily_bars (
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,              -- 'domestic' | 'overseas'
    date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (symbol, market, date)
);

-- 백테스팅용 전체 OHLCV 일봉 캐시. 과거 일봉 지표(RSI/MACD/BB/ATR) 재현용.
CREATE TABLE IF NOT EXISTS backtest_bars (
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,              -- 'domestic' | 'overseas'
    date TEXT NOT NULL,                -- 'YYYY-MM-DD'
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, market, date)
);
CREATE INDEX IF NOT EXISTS idx_backtest_bars_date ON backtest_bars(date);

-- AI 종목 발굴 및 뉴스/리포트 분석 묶음 (발굴 갱신 1회 = 1묶음)
CREATE TABLE IF NOT EXISTS insight_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,          -- 'SCHEDULED' | 'MANUAL'
    status TEXT NOT NULL,                -- RUNNING | DONE | PARTIAL | FAILED
    started_at REAL NOT NULL,
    finished_at REAL,
    window_start REAL,                   -- 보유종목 뉴스 집계 구간 시작
    counts_json TEXT,                    -- {broker_report, new_report, market_headline, holding_news, candidate, shortlist}
    refresh_summary_json TEXT,           -- 발굴 summary 스냅샷
    digest_json TEXT,                    -- LLM 다이제스트 (headline, themes, notable_symbols, risks, holdings_watch)
    digest_error TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_insight_batches_started ON insight_batches(started_at DESC);

-- 묶음 안의 개별 항목 (리포트/헤드라인/보유뉴스/발굴후보)
CREATE TABLE IF NOT EXISTS insight_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES insight_batches(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                  -- broker_report | market_headline | holding_news | candidate
    symbol TEXT,
    name TEXT,
    title TEXT,
    source TEXT,
    url TEXT,
    published_at REAL,
    ref_id TEXT,                         -- broker_report: research_id
    is_new INTEGER NOT NULL DEFAULT 0,   -- 이 묶음에서 처음 본 항목
    payload_json TEXT                    -- 상세 데이터 JSON
);
CREATE INDEX IF NOT EXISTS idx_insight_items_batch ON insight_items(batch_id, kind);
CREATE INDEX IF NOT EXISTS idx_insight_items_symbol ON insight_items(symbol);

-- 증권사 리포트 본문 및 LLM 분석 캐시
CREATE TABLE IF NOT EXISTS research_reports (
    research_id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT,
    broker TEXT,
    title TEXT,
    write_date TEXT,
    read_count INTEGER,
    opinion TEXT,
    target_price REAL,
    price_at_write REAL,
    content_text TEXT,
    attach_url TEXT,
    end_url TEXT,
    analysis_json TEXT,                  -- {stance, summary, key_points, catalysts, risks}
    analyzed_at REAL,
    fetched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_reports_symbol ON research_reports(symbol);
CREATE INDEX IF NOT EXISTS idx_research_reports_date ON research_reports(write_date DESC);

-- 배당 및 ETF 분배금 이력 (현금배당, ETF분배금, 주식배당)
CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'domestic',     -- 'domestic' | 'overseas'
    dividend_type TEXT NOT NULL DEFAULT 'CASH',  -- 'CASH' | 'ETF_DIST' | 'STOCK'
    record_date TEXT,                            -- 배당기준일 (YYYY-MM-DD)
    payment_date TEXT NOT NULL,                  -- 실제 지급/입금일 (YYYY-MM-DD)
    qty REAL NOT NULL DEFAULT 0,                 -- 배당 당시 보유 수량
    dps REAL,                                    -- 주당 배당금 (Dividend Per Share)
    gross_amount REAL NOT NULL,                  -- 세전 배당금액
    tax_amount REAL NOT NULL DEFAULT 0,          -- 배당소득세 등 원천징수 세금
    net_amount REAL NOT NULL,                    -- 세후 실수령액
    currency TEXT NOT NULL DEFAULT 'KRW',        -- KRW | USD
    fx_rate REAL NOT NULL DEFAULT 1.0,           -- 지급일 기준 환율
    net_amount_krw REAL NOT NULL,                -- KRW 환산 세후 실수령액
    source TEXT NOT NULL DEFAULT 'MANUAL',       -- 'AUTO_KIS' | 'MANUAL' | 'CSV'
    kis_mgmt_no TEXT,                            -- KIS 중복 방지 식별 키
    notes TEXT,                                  -- 메모
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dividends_symbol ON dividends(symbol);
CREATE INDEX IF NOT EXISTS idx_dividends_payment_date ON dividends(payment_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dividends_kis_mgmt ON dividends(kis_mgmt_no) WHERE kis_mgmt_no IS NOT NULL;
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
    if "winding_down_at" not in columns:
        await conn.execute("ALTER TABLE portfolio_symbols ADD COLUMN winding_down_at REAL")
    if "winddown_sell_started_at" not in columns:
        await conn.execute("ALTER TABLE portfolio_symbols ADD COLUMN winddown_sell_started_at REAL")

    cur = await conn.execute("PRAGMA table_info(candidate_universe)")
    columns = {row["name"] for row in await cur.fetchall()}
    for col, col_type in (("sources_json", "TEXT"), ("last_seen_at", "REAL"), ("expires_at", "REAL")):
        if col not in columns:
            await conn.execute(f"ALTER TABLE candidate_universe ADD COLUMN {col} {col_type}")


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
        "ON CONFLICT(symbol) DO UPDATE SET enabled = 1, market = excluded.market, exchange = excluded.exchange, "
        "disabled_at = NULL, winding_down_at = NULL, winddown_sell_started_at = NULL",
        (symbol, market, exchange, time.time()),
    )
    await conn.commit()


async def start_symbol_winddown(conn: aiosqlite.Connection, symbol: str) -> bool:
    """사용자가 포트폴리오에서 종목을 삭제했지만 보유분이 남아있을 때 "정리 대기"로 표시한다.
    즉시 청산이 아니다 — 엔진은 신규매수를 멈추고, 매도 신호가 오거나 기한(WINDDOWN_MAX_DAYS)이
    지나면 전량 매도한다. 이미 정리 대기 중이면 시작 시각을 유지한다(기한 연장 방지)."""
    cur = await conn.execute(
        "UPDATE portfolio_symbols SET winding_down_at = COALESCE(winding_down_at, ?) "
        "WHERE symbol = ? AND enabled = 1", (time.time(), symbol),
    )
    await conn.commit()
    return cur.rowcount > 0


async def mark_winddown_sell_started(conn: aiosqlite.Connection, symbol: str) -> None:
    """첫 정리 매도가 나가면 표시한다 — 1회 주문 한도로 분할 매도되는 동안 신호가 HOLD로
    돌아서도 잔여분이 남지 않고 끝까지 청산되도록 한다."""
    await conn.execute(
        "UPDATE portfolio_symbols SET winddown_sell_started_at = COALESCE(winddown_sell_started_at, ?) "
        "WHERE symbol = ? AND winding_down_at IS NOT NULL", (time.time(), symbol),
    )
    await conn.commit()


async def finish_symbol_removal(conn: aiosqlite.Connection, symbol: str) -> None:
    """포트폴리오에서 완전히 제거: 비활성화 + 목표비중 삭제. 이력(target_weights/decision_log)은 보존."""
    await conn.execute(
        "UPDATE portfolio_symbols SET enabled = 0, disabled_at = ?, winding_down_at = NULL, "
        "winddown_sell_started_at = NULL WHERE symbol = ?", (time.time(), symbol),
    )
    await conn.execute("DELETE FROM active_target_weights WHERE symbol = ?", (symbol,))
    await conn.commit()


async def list_recently_removed_symbols(conn: aiosqlite.Connection, within_days: int) -> List[str]:
    """최근 within_days일 안에 포트폴리오에서 제거(비활성화)된 종목코드. 삭제한 종목이 발굴
    후보로 되살아나 AI가 다시 편입 제안하는 것을 막는 데 쓴다. 재등록하면 disabled_at이
    NULL로 돌아가므로 다시 담은 종목은 포함되지 않는다."""
    cutoff = time.time() - within_days * 86400
    cur = await conn.execute(
        "SELECT symbol FROM portfolio_symbols WHERE enabled = 0 AND disabled_at IS NOT NULL AND disabled_at >= ?",
        (cutoff,),
    )
    return [row["symbol"] for row in await cur.fetchall()]


async def list_portfolio_symbols(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM portfolio_symbols WHERE enabled = 1")
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def get_portfolio_symbol_info(conn: aiosqlite.Connection) -> Dict[str, Dict[str, Any]]:
    """symbol -> {"market": ..., "exchange": ...}. 사이클에서 국내/해외 처리 경로를 나누는 데 사용."""
    rows = await list_portfolio_symbols(conn)
    return {
        row["symbol"]: {
            "market": row["market"], "exchange": row["exchange"],
            "winding_down_at": row["winding_down_at"], "winddown_sell_started_at": row["winddown_sell_started_at"],
        }
        for row in rows
    }


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
    """시드/수동 후보. 이미 동적 후보로 들어와 있던 종목이면 만료 없는 영구 후보로 승격한다."""
    now = time.time()
    await conn.execute(
        "INSERT INTO candidate_universe(symbol, market, exchange, name, universe_tag, validated_at, "
        "added_at, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, validated_at=excluded.validated_at, enabled=1, "
        "universe_tag=CASE WHEN candidate_universe.universe_tag = 'DYNAMIC' THEN excluded.universe_tag "
        "ELSE candidate_universe.universe_tag END, expires_at=NULL",
        (symbol, market, exchange, name, universe_tag, now, now),
    )
    await conn.commit()


async def upsert_dynamic_candidate(
    conn: aiosqlite.Connection, symbol: str, name: str, sources_json: str, expires_at: float,
) -> None:
    """동적 발굴 후보. 시드/수동 후보(expires_at IS NULL)와 겹치면 태그/만료는 건드리지 않고
    발굴 근거(sources_json)만 갱신한다 — 영구 후보가 동적 후보로 강등되어 만료되면 안 된다."""
    now = time.time()
    await conn.execute(
        "INSERT INTO candidate_universe(symbol, market, name, universe_tag, validated_at, added_at, enabled, "
        "sources_json, last_seen_at, expires_at) VALUES (?, 'domestic', ?, 'DYNAMIC', ?, ?, 1, ?, ?, ?) "
        "ON CONFLICT(symbol) DO UPDATE SET sources_json=excluded.sources_json, last_seen_at=excluded.last_seen_at, "
        "enabled=CASE WHEN candidate_universe.expires_at IS NULL THEN candidate_universe.enabled ELSE 1 END, "
        "expires_at=CASE WHEN candidate_universe.expires_at IS NULL THEN NULL ELSE excluded.expires_at END",
        (symbol, name, now, now, sources_json, now, expires_at),
    )
    await conn.commit()


async def disable_expired_candidates(conn: aiosqlite.Connection, now: float) -> int:
    cur = await conn.execute(
        "UPDATE candidate_universe SET enabled = 0 WHERE enabled = 1 AND expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    await conn.commit()
    return cur.rowcount


async def trim_dynamic_candidates(conn: aiosqlite.Connection, keep: int) -> int:
    """활성 동적 후보를 최근에 다시 잡힌 순으로 keep개만 남긴다 — 점수 계산 비용(종목당 KIS 여러 번)이
    TTL 기간 동안 누적되는 후보 수에 비례해 끝없이 늘지 않게 하기 위함."""
    cur = await conn.execute(
        "UPDATE candidate_universe SET enabled = 0 WHERE symbol IN ("
        "  SELECT symbol FROM candidate_universe WHERE enabled = 1 AND expires_at IS NOT NULL "
        "  ORDER BY last_seen_at DESC LIMIT -1 OFFSET ?)",
        (keep,),
    )
    await conn.commit()
    return cur.rowcount


async def disable_candidate_symbol(conn: aiosqlite.Connection, symbol: str) -> bool:
    """사람이 직접 뺀 후보. expires_at을 NULL로 만들어 다음 동적 갱신이 다시 켜지 않게 한다
    (다시 넣으려면 수동 추가)."""
    cur = await conn.execute(
        "UPDATE candidate_universe SET enabled = 0, expires_at = NULL WHERE symbol = ? AND enabled = 1", (symbol,)
    )
    await conn.commit()
    return cur.rowcount > 0


async def list_candidate_universe(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM candidate_universe WHERE enabled = 1")
    rows = await cur.fetchall()
    return [dict(row) for row in rows]


# --- stock_master / stock_theme (KIS 마스터 파일) ---

async def replace_stock_master(
    conn: aiosqlite.Connection, stocks: List[Dict[str, Any]], themes: List[Dict[str, str]],
) -> None:
    now = time.time()
    await conn.execute("DELETE FROM stock_master")
    await conn.executemany(
        "INSERT OR REPLACE INTO stock_master(symbol, name, market, sector_code, market_cap_eok, roe, "
        "is_excluded, refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(s["symbol"], s["name"], s["market"], s["sector_code"], s["market_cap_eok"], s["roe"],
          1 if s["is_excluded"] else 0, now) for s in stocks],
    )
    if themes:
        await conn.execute("DELETE FROM stock_theme")
        await conn.executemany(
            "INSERT OR IGNORE INTO stock_theme(theme_code, theme_name, symbol) VALUES (?, ?, ?)",
            [(t["theme_code"], t["theme_name"], t["symbol"]) for t in themes],
        )
    await conn.commit()


async def get_stock_master_refreshed_at(conn: aiosqlite.Connection) -> Optional[float]:
    cur = await conn.execute("SELECT MAX(refreshed_at) AS ts FROM stock_master")
    row = await cur.fetchone()
    return row["ts"] if row else None


async def get_stock_master(conn: aiosqlite.Connection) -> Dict[str, Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM stock_master")
    return {row["symbol"]: dict(row) for row in await cur.fetchall()}


async def get_theme_memberships(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    cur = await conn.execute("SELECT theme_code, theme_name, symbol FROM stock_theme")
    return [dict(row) for row in await cur.fetchall()]


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


async def sync_positions_from_balance(
    conn: aiosqlite.Connection, held: Dict[str, Tuple[float, float, str]], overseas: bool
) -> None:
    """브로커 잔고(ground truth)로 positions를 동기화한다. held: symbol -> (qty, avg_price, currency).

    전량 매도한 종목은 잔고 응답에서 아예 사라지므로, "응답에 있는 종목만 upsert"하면 청산이
    영영 반영되지 않는다(qty/진입가/고점이 남아 재진입 시 옛 고점이 이월됨). 그래서 같은 시장
    (국내=KRW, 해외=그 외 통화)의 기존 qty>0 행 중 응답에 없는 종목은 qty=0으로 내려
    upsert_position의 "완전청산 -> 진입 상태 초기화" 분기를 태운다.
    이것은 "보유 사실"만 다룬다 — 관심/제외 여부(active_target_weights)와는 무관하다."""
    for symbol, (qty, avg_price, currency) in held.items():
        await upsert_position(conn, symbol, qty, avg_price, currency=currency)
    cur = await conn.execute("SELECT symbol, currency FROM positions WHERE qty > 0")
    for row in await cur.fetchall():
        if row["symbol"] in held or (row["currency"] != "KRW") != overseas:
            continue
        await upsert_position(conn, row["symbol"], 0.0, 0.0, currency=row["currency"])


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


async def record_rest_fill(conn: aiosqlite.Connection, intent_id: str, total_qty: float, price: float) -> None:
    """REST 체결내역 기준 누적 체결수량/평균가를 fills에 반영한다. 웹소켓이 놓친 체결을 채우기 위한 것.
    이미 기록된 수량(WS 포함)을 넘는 증분만 넣어 중복 기록을 피한다."""
    if total_qty <= 0 or price <= 0:
        return
    cur = await conn.execute("SELECT COALESCE(SUM(qty), 0) AS q FROM fills WHERE intent_id = ?", (intent_id,))
    recorded = float((await cur.fetchone())["q"])
    if total_qty <= recorded:
        return
    await conn.execute(
        "INSERT INTO fills(intent_id, qty, price, filled_at, source) VALUES (?, ?, ?, ?, 'REST_POLL')",
        (intent_id, total_qty - recorded, price, time.time()),
    )
    await conn.commit()


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


async def get_unresolved_intents(conn: aiosqlite.Connection, min_age_sec: float = 0.0) -> List[Dict[str, Any]]:
    """reconciliation 대상: 아직 최종 상태가 아닌 intent들. min_age_sec>0이면 방금 낸 주문
    (브로커 체결내역에 아직 안 잡혔을 수 있음)은 제외한다 — 사이클 중 주기 대조용."""
    cur = await conn.execute(
        "SELECT * FROM order_intents WHERE status IN ('PENDING', 'SUBMITTED') AND created_at <= ?",
        (time.time() - min_age_sec,),
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


# --- analytics (읽기 전용 조회: 분석 대시보드용) ---

async def list_all_portfolio_symbols(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """비활성(편입 제외된) 종목까지 포함. 과거 체결 종목의 market/exchange를 찾는 데 쓴다."""
    cur = await conn.execute("SELECT * FROM portfolio_symbols")
    return [dict(row) for row in await cur.fetchall()]


async def list_order_intents_since(conn: aiosqlite.Connection, since_ts: float) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM order_intents WHERE created_at >= ? ORDER BY created_at ASC", (since_ts,)
    )
    return [dict(row) for row in await cur.fetchall()]


async def list_fills_for_intents(conn: aiosqlite.Connection, intent_ids: List[str]) -> List[Dict[str, Any]]:
    if not intent_ids:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(0, len(intent_ids), 500):  # SQLite 변수 개수 제한 회피
        chunk = intent_ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = await conn.execute(
            f"SELECT * FROM fills WHERE intent_id IN ({placeholders}) ORDER BY filled_at ASC", chunk
        )
        out.extend(dict(row) for row in await cur.fetchall())
    return out


async def list_decision_contexts_with_intent(conn: aiosqlite.Connection, since_ts: float) -> List[Dict[str, Any]]:
    """주문으로 이어진 결정(intent_id 있음)의 컨텍스트. fills가 없는 체결의 가격/원가 추정에 쓴다."""
    cur = await conn.execute(
        "SELECT intent_id, ts, context_json FROM decision_log WHERE intent_id IS NOT NULL AND ts >= ?",
        (since_ts,),
    )
    return [dict(row) for row in await cur.fetchall()]


async def list_context_rows(conn: aiosqlite.Connection, since_ts: float) -> List[Dict[str, Any]]:
    """환율(bass_exrt)·총자산(total_equity) 시계열 추출용. 두 값은 decision_log.context_json에만 남는다."""
    cur = await conn.execute(
        "SELECT ts, context_json FROM decision_log "
        "WHERE ts >= ? AND (context_json LIKE '%bass_exrt%' OR context_json LIKE '%total_equity%') "
        "ORDER BY ts ASC",
        (since_ts,),
    )
    return [dict(row) for row in await cur.fetchall()]


async def list_decisions_light(
    conn: aiosqlite.Connection, symbol: Optional[str], since_ts: float, until_ts: float, include_noop: bool
) -> List[Dict[str, Any]]:
    """복기용 결정 목록. context_json은 제외(상세 조회에서만)하고 주문 상태/가격을 조인한다."""
    sql = (
        "SELECT d.id, d.cycle_id, d.symbol, d.ts, d.current_weight, d.target_weight, d.drift, "
        "d.tech_signal, d.sentiment_signal, d.action, d.qty, d.reason, d.intent_id, "
        "oi.status AS intent_status, oi.side AS intent_side, oi.price AS intent_price, oi.market AS market, "
        "json_extract(d.context_json, '$.price') AS ctx_price, "
        "json_extract(d.context_json, '$.price_foreign') AS ctx_price_foreign "
        "FROM decision_log d LEFT JOIN order_intents oi ON oi.intent_id = d.intent_id "
        "WHERE d.ts >= ? AND d.ts <= ?"
    )
    params: List[Any] = [since_ts, until_ts]
    if symbol:
        sql += " AND d.symbol = ?"
        params.append(symbol)
    if not include_noop:
        sql += " AND d.action != 'NO_OP'"
    sql += " ORDER BY d.ts ASC"
    cur = await conn.execute(sql, params)
    return [dict(row) for row in await cur.fetchall()]


async def get_decision(conn: aiosqlite.Connection, decision_id: int) -> Optional[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT d.*, oi.status AS intent_status, oi.price AS intent_price, oi.qty AS intent_qty "
        "FROM decision_log d LEFT JOIN order_intents oi ON oi.intent_id = d.intent_id WHERE d.id = ?",
        (decision_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_daily_bars(
    conn: aiosqlite.Connection, symbol: str, market: str, since_date: str
) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT date, close FROM daily_bars WHERE symbol = ? AND market = ? AND date >= ? ORDER BY date ASC",
        (symbol, market, since_date),
    )
    return [dict(row) for row in await cur.fetchall()]


async def save_daily_bars(
    conn: aiosqlite.Connection, symbol: str, market: str, bars: List[Dict[str, Any]]
) -> None:
    for bar in bars:
        await conn.execute(
            "INSERT INTO daily_bars(symbol, market, date, close) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol, market, date) DO UPDATE SET close=excluded.close",
            (symbol, market, bar["date"], bar["close"]),
        )
    await conn.commit()


async def list_approved_weight_events(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """비중 타임라인용: 적용된(APPROVED/AUTO_APPLIED) 리밸런싱 이벤트를 시간순으로."""
    cur = await conn.execute(
        "SELECT * FROM rebalance_events WHERE status IN ('APPROVED','AUTO_APPLIED') ORDER BY COALESCE(decided_at, created_at) ASC"
    )
    return [dict(row) for row in await cur.fetchall()]


async def get_data_start_ts(conn: aiosqlite.Connection) -> Optional[float]:
    """엔진이 처음 사이클을 돈 시각. 그 이전의 보유/가격 변동은 이 DB로 알 수 없다."""
    cur = await conn.execute("SELECT MIN(started_at) AS t FROM cycles")
    row = await cur.fetchone()
    return row["t"] if row and row["t"] is not None else None


async def get_backtest_bars(
    conn: aiosqlite.Connection, symbol: str, market: str, start_date: str, end_date: str
) -> List[Dict[str, Any]]:
    """백테스트용 OHLCV 일봉 조회. date 오름차순 정렬."""
    cur = await conn.execute(
        "SELECT symbol, market, date, open, high, low, close, volume "
        "FROM backtest_bars "
        "WHERE symbol = ? AND market = ? AND date >= ? AND date <= ? "
        "ORDER BY date ASC",
        (symbol, market, start_date, end_date),
    )
    return [dict(row) for row in await cur.fetchall()]


async def save_backtest_bars(
    conn: aiosqlite.Connection, symbol: str, market: str, bars: List[Dict[str, Any]]
) -> None:
    """백테스트용 OHLCV 일봉 일괄 저장."""
    for bar in bars:
        await conn.execute(
            "INSERT INTO backtest_bars(symbol, market, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, market, date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume",
            (
                symbol,
                market,
                bar["date"],
                bar["open"],
                bar["high"],
                bar["low"],
                bar["close"],
                bar.get("volume", 0.0),
            ),
        )
    await conn.commit()


# --- insight_batches & insight_items & research_reports ---

async def create_insight_batch(
    conn: aiosqlite.Connection,
    trigger_type: str = "SCHEDULED",
    started_at: Optional[float] = None,
    window_start: Optional[float] = None,
) -> int:
    started_at = started_at or time.time()
    cur = await conn.execute(
        "INSERT INTO insight_batches(trigger_type, status, started_at, window_start) "
        "VALUES (?, 'RUNNING', ?, ?)",
        (trigger_type, started_at, window_start),
    )
    await conn.commit()
    return cur.lastrowid


async def finish_insight_batch(
    conn: aiosqlite.Connection,
    batch_id: int,
    status: str,
    finished_at: Optional[float] = None,
    counts_json: Optional[str] = None,
    refresh_summary_json: Optional[str] = None,
    digest_json: Optional[str] = None,
    digest_error: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    finished_at = finished_at or time.time()
    await conn.execute(
        "UPDATE insight_batches SET status = ?, finished_at = ?, counts_json = ?, "
        "refresh_summary_json = ?, digest_json = ?, digest_error = ?, error = ? "
        "WHERE id = ?",
        (status, finished_at, counts_json, refresh_summary_json, digest_json, digest_error, error, batch_id),
    )
    await conn.commit()


async def add_insight_items(conn: aiosqlite.Connection, items: List[Dict[str, Any]]) -> None:
    if not items:
        return
    rows = [
        (
            item["batch_id"],
            item["kind"],
            item.get("symbol"),
            item.get("name"),
            item.get("title"),
            item.get("source"),
            item.get("url"),
            item.get("published_at"),
            str(item["ref_id"]) if item.get("ref_id") is not None else None,
            1 if item.get("is_new") else 0,
            item.get("payload_json"),
        )
        for item in items
    ]
    await conn.executemany(
        "INSERT INTO insight_items(batch_id, kind, symbol, name, title, source, url, "
        "published_at, ref_id, is_new, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await conn.commit()


async def list_insight_batches(conn: aiosqlite.Connection, limit: int = 30) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT id, trigger_type, status, started_at, finished_at, window_start, counts_json, "
        "refresh_summary_json, digest_json, digest_error, error "
        "FROM insight_batches ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_insight_batch(conn: aiosqlite.Connection, batch_id: int) -> Optional[Dict[str, Any]]:
    cur = await conn.execute("SELECT * FROM insight_batches WHERE id = ?", (batch_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_insight_items(
    conn: aiosqlite.Connection, batch_id: int, kind: Optional[str] = None
) -> List[Dict[str, Any]]:
    if kind:
        cur = await conn.execute(
            "SELECT * FROM insight_items WHERE batch_id = ? AND kind = ? ORDER BY id ASC",
            (batch_id, kind),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM insight_items WHERE batch_id = ? ORDER BY id ASC",
            (batch_id,),
        )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def list_symbol_insights(
    conn: aiosqlite.Connection, symbol: str, limit: int = 50
) -> List[Dict[str, Any]]:
    cur = await conn.execute(
        "SELECT i.*, b.started_at as batch_started_at, b.trigger_type as batch_trigger_type "
        "FROM insight_items i JOIN insight_batches b ON i.batch_id = b.id "
        "WHERE i.symbol = ? ORDER BY i.id DESC LIMIT ?",
        (symbol, limit),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_research_reports(
    conn: aiosqlite.Connection, research_ids: List[int]
) -> Dict[int, Dict[str, Any]]:
    if not research_ids:
        return {}
    placeholders = ", ".join("?" for _ in research_ids)
    cur = await conn.execute(
        f"SELECT * FROM research_reports WHERE research_id IN ({placeholders})",
        research_ids,
    )
    rows = await cur.fetchall()
    return {row["research_id"]: dict(row) for row in rows}


async def upsert_research_reports(conn: aiosqlite.Connection, reports: List[Dict[str, Any]]) -> None:
    if not reports:
        return
    now = time.time()
    for r in reports:
        await conn.execute(
            "INSERT INTO research_reports("
            "research_id, symbol, name, broker, title, write_date, read_count, "
            "opinion, target_price, price_at_write, content_text, attach_url, end_url, fetched_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(research_id) DO UPDATE SET "
            "read_count = excluded.read_count, "
            "opinion = COALESCE(excluded.opinion, research_reports.opinion), "
            "target_price = COALESCE(excluded.target_price, research_reports.target_price), "
            "price_at_write = COALESCE(excluded.price_at_write, research_reports.price_at_write), "
            "content_text = COALESCE(excluded.content_text, research_reports.content_text), "
            "attach_url = COALESCE(excluded.attach_url, research_reports.attach_url), "
            "end_url = COALESCE(excluded.end_url, research_reports.end_url)",
            (
                r["research_id"],
                r.get("symbol") or "",
                r.get("name"),
                r.get("broker"),
                r.get("title"),
                r.get("write_date") or r.get("date"),
                r.get("read_count", 0),
                r.get("opinion"),
                r.get("target_price"),
                r.get("price_at_write"),
                r.get("content_text"),
                r.get("attach_url"),
                r.get("end_url"),
                now,
            ),
        )
    await conn.commit()


async def update_research_report_detail(
    conn: aiosqlite.Connection,
    research_id: int,
    content_text: str,
    opinion: Optional[str] = None,
    target_price: Optional[float] = None,
    price_at_write: Optional[float] = None,
    attach_url: Optional[str] = None,
) -> None:
    await conn.execute(
        "UPDATE research_reports SET "
        "content_text = COALESCE(?, content_text), "
        "opinion = COALESCE(?, opinion), "
        "target_price = COALESCE(?, target_price), "
        "price_at_write = COALESCE(?, price_at_write), "
        "attach_url = COALESCE(?, attach_url) "
        "WHERE research_id = ?",
        (content_text, opinion, target_price, price_at_write, attach_url, research_id),
    )
    await conn.commit()


async def save_report_analysis(
    conn: aiosqlite.Connection,
    research_id: int,
    analysis_json: str,
    analyzed_at: Optional[float] = None,
) -> None:
    analyzed_at = analyzed_at or time.time()
    await conn.execute(
        "UPDATE research_reports SET analysis_json = ?, analyzed_at = ? WHERE research_id = ?",
        (analysis_json, analyzed_at, research_id),
    )
    await conn.commit()


async def get_last_insight_batch_finished_at(conn: aiosqlite.Connection) -> Optional[float]:
    cur = await conn.execute(
        "SELECT finished_at FROM insight_batches WHERE status IN ('DONE', 'PARTIAL') "
        "AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
    )
    row = await cur.fetchone()
    return float(row["finished_at"]) if row and row["finished_at"] is not None else None


async def cleanup_old_insights(conn: aiosqlite.Connection, retention_days: int = 365) -> int:
    """1년(retention_days) 지난 인사이트 묶음 및 하위 항목을 정리한다."""
    cutoff = time.time() - retention_days * 86400
    cur = await conn.execute(
        "SELECT id FROM insight_batches WHERE started_at < ?", (cutoff,)
    )
    old_ids = [r["id"] for r in await cur.fetchall()]
    if not old_ids:
        return 0
    placeholders = ", ".join("?" for _ in old_ids)
    await conn.execute(f"DELETE FROM insight_items WHERE batch_id IN ({placeholders})", old_ids)
    await conn.execute(f"DELETE FROM insight_batches WHERE id IN ({placeholders})", old_ids)
    await conn.commit()
    return len(old_ids)



async def get_symbol_semantic_profile(
    conn: aiosqlite.Connection, symbol: str, lookback_days: int = 7
) -> Dict[str, Any]:
    """종목의 최근 뉴스 감성 및 증권사 리포트 LLM 분석 결과를 통합한 정형 시맨틱 프로필을 반환한다."""
    cutoff_ts = time.time() - lookback_days * 86400

    # 1. 뉴스 감성 집계 (news_cache)
    cur = await conn.execute(
        "SELECT sentiment_score, sentiment_reasoning, fetched_at "
        "FROM news_cache WHERE symbol = ? AND fetched_at >= ? "
        "ORDER BY fetched_at DESC LIMIT 20",
        (symbol, cutoff_ts),
    )
    news_rows = await cur.fetchall()
    news_scores = [float(r["sentiment_score"]) for r in news_rows if r["sentiment_score"] is not None]
    avg_news_sentiment = (sum(news_scores) / len(news_scores)) if news_scores else None
    latest_news_reasoning = news_rows[0]["sentiment_reasoning"] if news_rows else None

    # 2. 증권사 리포트 분석 (research_reports)
    cutoff_date = (datetime.now(tz=ZoneInfo("Asia/Seoul")) - timedelta(days=14)).strftime("%Y%m%d")
    cur = await conn.execute(
        "SELECT broker, title, opinion, target_price, price_at_write, write_date, analysis_json "
        "FROM research_reports WHERE symbol = ? AND write_date >= ? "
        "ORDER BY write_date DESC LIMIT 5",
        (symbol, cutoff_date),
    )
    report_rows = await cur.fetchall()

    latest_stance = None
    latest_summary = None
    target_price = None
    stance_counts = {"POSITIVE": 0, "NEUTRAL": 0, "CAUTION": 0}

    for r in report_rows:
        if target_price is None and r["target_price"]:
            try:
                target_price = float(r["target_price"])
            except (ValueError, TypeError):
                pass
        if r["analysis_json"]:
            try:
                ad = json.loads(r["analysis_json"])
                st = (ad.get("stance") or "").upper()
                if st in stance_counts:
                    stance_counts[st] += 1
                if latest_stance is None and st:
                    latest_stance = st
                    latest_summary = ad.get("summary")
            except Exception:
                pass

    return {
        "symbol": symbol,
        "news_count": len(news_rows),
        "avg_news_sentiment": avg_news_sentiment,
        "latest_news_reasoning": latest_news_reasoning,
        "report_count": len(report_rows),
        "latest_report_stance": latest_stance,
        "latest_report_summary": latest_summary,
        "report_stance_counts": stance_counts,
        "target_price": target_price,
    }


async def get_latest_insight_digest(conn: aiosqlite.Connection) -> Optional[Dict[str, Any]]:
    """가장 최근 완료된 인사이트 배치의 AI 다이제스트 JSON을 파싱하여 반환한다."""
    cur = await conn.execute(
        "SELECT digest_json FROM insight_batches WHERE status IN ('DONE', 'PARTIAL') "
        "AND digest_json IS NOT NULL ORDER BY started_at DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row or not row["digest_json"]:
        return None
    try:
        return json.loads(row["digest_json"])
    except Exception:
        return None


# =========================================================================
# 배당 및 ETF 분배금 관리
# =========================================================================

async def add_dividend(conn: aiosqlite.Connection, data: Dict[str, Any]) -> int:
    """새 배당/분배금 기록을 추가하고 id를 반환한다."""
    now = time.time()
    gross = float(data.get("gross_amount") or 0.0)
    tax = float(data.get("tax_amount") or 0.0)
    net = float(data.get("net_amount") if data.get("net_amount") is not None else (gross - tax))
    fx_rate = float(data.get("fx_rate") or 1.0)
    net_krw = float(data.get("net_amount_krw") if data.get("net_amount_krw") is not None else (net * fx_rate))

    cur = await conn.execute(
        """
        INSERT INTO dividends (
            symbol, market, dividend_type, record_date, payment_date,
            qty, dps, gross_amount, tax_amount, net_amount,
            currency, fx_rate, net_amount_krw, source, kis_mgmt_no,
            notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["symbol"],
            data.get("market") or "domestic",
            data.get("dividend_type") or "CASH",
            data.get("record_date"),
            data["payment_date"],
            float(data.get("qty") or 0.0),
            float(data["dps"]) if data.get("dps") is not None else None,
            gross,
            tax,
            net,
            data.get("currency") or "KRW",
            fx_rate,
            net_krw,
            data.get("source") or "MANUAL",
            data.get("kis_mgmt_no"),
            data.get("notes"),
            now,
            now,
        ),
    )
    await conn.commit()
    return cur.lastrowid or 0


async def get_dividend(conn: aiosqlite.Connection, dividend_id: int) -> Optional[Dict[str, Any]]:
    """배당 기록 단건 조회."""
    cur = await conn.execute("SELECT * FROM dividends WHERE id = ?", (dividend_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_dividends(
    conn: aiosqlite.Connection,
    symbol: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """조건에 맞는 배당/분배금 목록을 최신 지급일 순으로 조회."""
    clauses: List[str] = []
    params: List[Any] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if start_date:
        clauses.append("payment_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("payment_date <= ?")
        params.append(end_date)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT * FROM dividends {where} ORDER BY payment_date DESC, id DESC"
    cur = await conn.execute(query, params)
    return [dict(r) for r in await cur.fetchall()]


async def update_dividend(conn: aiosqlite.Connection, dividend_id: int, data: Dict[str, Any]) -> bool:
    """배당 기록 수정."""
    now = time.time()
    fields = []
    params = []
    updatable = [
        "symbol", "market", "dividend_type", "record_date", "payment_date",
        "qty", "dps", "gross_amount", "tax_amount", "net_amount",
        "currency", "fx_rate", "net_amount_krw", "notes", "source",
    ]
    for key in updatable:
        if key in data:
            fields.append(f"{key} = ?")
            params.append(data[key])
    if not fields:
        return False
    fields.append("updated_at = ?")
    params.append(now)
    params.append(dividend_id)

    cur = await conn.execute(
        f"UPDATE dividends SET {', '.join(fields)} WHERE id = ?", params
    )
    await conn.commit()
    return cur.rowcount > 0


async def delete_dividend(conn: aiosqlite.Connection, dividend_id: int) -> bool:
    """배당 기록 삭제."""
    cur = await conn.execute("DELETE FROM dividends WHERE id = ?", (dividend_id,))
    await conn.commit()
    return cur.rowcount > 0


async def get_dividend_summary_by_symbol(conn: aiosqlite.Connection) -> Dict[str, Dict[str, Any]]:
    """종목별 배당금 합계(세후, 세전, 세금) 및 건수 집계."""
    cur = await conn.execute(
        """
        SELECT symbol,
               SUM(net_amount_krw) as total_net_krw,
               SUM(gross_amount) as total_gross,
               SUM(tax_amount) as total_tax,
               COUNT(*) as count,
               MAX(payment_date) as last_payment_date
        FROM dividends
        GROUP BY symbol
        """
    )
    rows = await cur.fetchall()
    return {
        r["symbol"]: {
            "total_net_krw": float(r["total_net_krw"] or 0.0),
            "total_gross": float(r["total_gross"] or 0.0),
            "total_tax": float(r["total_tax"] or 0.0),
            "count": int(r["count"]),
            "last_payment_date": r["last_payment_date"],
        }
        for r in rows
    }


async def get_monthly_dividends(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """월별 배당금 합계(세후, 세전, 세금) 및 건수 집계."""
    cur = await conn.execute(
        """
        SELECT SUBSTR(payment_date, 1, 7) as month,
               SUM(net_amount_krw) as total_net_krw,
               SUM(gross_amount) as total_gross,
               SUM(tax_amount) as total_tax,
               COUNT(*) as count
        FROM dividends
        GROUP BY SUBSTR(payment_date, 1, 7)
        ORDER BY month ASC
        """
    )
    rows = await cur.fetchall()
    return [
        {
            "month": r["month"],
            "total_net_krw": float(r["total_net_krw"] or 0.0),
            "total_gross": float(r["total_gross"] or 0.0),
            "total_tax": float(r["total_tax"] or 0.0),
            "count": int(r["count"]),
        }
        for r in rows
    ]


async def upsert_kis_dividends(conn: aiosqlite.Connection, items: List[Dict[str, Any]]) -> Tuple[int, int]:
    """KIS API 등에서 수집한 배당 목록을 삽입/스킵한다. (inserted_count, skipped_count) 반환."""
    inserted = 0
    skipped = 0
    now = time.time()
    for it in items:
        kis_mgmt_no = it.get("kis_mgmt_no")
        if kis_mgmt_no:
            cur = await conn.execute("SELECT id FROM dividends WHERE kis_mgmt_no = ?", (kis_mgmt_no,))
            if await cur.fetchone():
                skipped += 1
                continue

        gross = float(it.get("gross_amount") or 0.0)
        tax = float(it.get("tax_amount") or 0.0)
        net = float(it.get("net_amount") if it.get("net_amount") is not None else (gross - tax))
        fx_rate = float(it.get("fx_rate") or 1.0)
        net_krw = float(it.get("net_amount_krw") if it.get("net_amount_krw") is not None else (net * fx_rate))

        await conn.execute(
            """
            INSERT INTO dividends (
                symbol, market, dividend_type, record_date, payment_date,
                qty, dps, gross_amount, tax_amount, net_amount,
                currency, fx_rate, net_amount_krw, source, kis_mgmt_no,
                notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                it["symbol"],
                it.get("market") or "domestic",
                it.get("dividend_type") or "CASH",
                it.get("record_date"),
                it["payment_date"],
                float(it.get("qty") or 0.0),
                float(it["dps"]) if it.get("dps") is not None else None,
                gross,
                tax,
                net,
                it.get("currency") or "KRW",
                fx_rate,
                net_krw,
                it.get("source") or "AUTO_KIS",
                kis_mgmt_no,
                it.get("notes"),
                now,
                now,
            ),
        )
        inserted += 1
    await conn.commit()
    return inserted, skipped


