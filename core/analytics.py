# core/analytics.py
"""분석 대시보드(app.py)용 조회 오케스트레이션: DB 읽기 + core.pnl 계산 + 일봉 캐시를 묶는다.

모든 함수는 읽기 전용이다(일봉 캐시 daily_bars 적재를 제외하고 거래/포지션 상태를 바꾸지 않는다).
엔드포인트(api/main.py)는 이 함수들을 얇게 감싼다.
"""
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from core import daily_prices, db, pnl
from core.config import settings
from core.kis_client import AsyncKISClient
from core.pnl import KST, parse_context

APPLIED_STATUSES = ("APPROVED", "AUTO_APPLIED")
PRICE_LOOKBACK_PAD_DAYS = 10  # 기간 첫날의 기준 종가(전일 종가)를 확보하기 위한 여유


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _date_to_ts(date_str: str, end_of_day: bool = False) -> float:
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    return (base + timedelta(days=1) - timedelta(seconds=1)).timestamp() if end_of_day else base.timestamp()


def _shift(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


# --- 공통 로딩 ---

async def _symbol_infos(conn: aiosqlite.Connection, symbols: List[str], default_market: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """{symbol: {market, exchange}} — 편입 제외된 종목도 포함해서 찾는다."""
    known = {r["symbol"]: r for r in await db.list_all_portfolio_symbols(conn)}
    out: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        row = known.get(sym)
        out[sym] = {
            "market": (row["market"] if row else None) or default_market.get(sym) or "domestic",
            "exchange": row["exchange"] if row else None,
        }
    return out


async def _load_trade_data(conn: aiosqlite.Connection) -> Dict[str, Any]:
    """전체 기간의 거래/오프닝/실현손익을 계산한다. (원가·오프닝 역산은 전체 이력이 필요하므로 기간 제한 없음)"""
    intents = await db.list_order_intents_since(conn, 0.0)
    fills = await db.list_fills_for_intents(conn, [i["intent_id"] for i in intents])
    ctx_rows = await db.list_decision_contexts_with_intent(conn, 0.0)
    ctx_by_intent = {r["intent_id"]: parse_context(r["context_json"]) for r in ctx_rows}
    fx_points, equity_points = pnl.extract_points(await db.list_context_rows(conn, 0.0))
    fx = pnl.make_fx(fx_points)

    trades, unpriced = pnl.build_trades(intents, fills, ctx_by_intent, fx)
    markets = {t["symbol"]: t["market"] for t in trades}
    positions = await db.get_positions(conn)
    pos_for_opening = {sym: {**p, "market": markets.get(sym) or ("overseas" if p.get("currency") == "USD" else "domestic")}
                       for sym, p in positions.items()}
    opening, mismatch = pnl.compute_opening(trades, pos_for_opening, fx)
    events, state = pnl.realized_events(trades, opening)
    for sym, p in pos_for_opening.items():
        markets.setdefault(sym, p["market"])
    return {
        "trades": trades, "unpriced": unpriced, "fx": fx, "has_fx": bool(fx_points), "equity_points": equity_points,
        "positions": positions, "markets": markets, "opening": opening, "mismatch": mismatch,
        "events": events, "state": state,
    }


def _clean(value: Any) -> Any:
    """NaN/inf는 JSON 직렬화가 안 되므로 None으로."""
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _clean_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: _clean(v) for k, v in r.items()} for r in rows]


# --- 오버뷰 (종목축 / 날짜축 / 종목x날짜) ---

async def build_overview(
    conn: aiosqlite.Connection, client: Optional[AsyncKISClient], days: int = 30, refresh: bool = False,
    fetch_prices: bool = True,
) -> Dict[str, Any]:
    data = await _load_trade_data(conn)
    trades, markets = data["trades"], data["markets"]
    today = _today()
    start = _shift(today, -max(days, 1))
    data_start_ts = await db.get_data_start_ts(conn)
    data_start = pnl.kst_date(data_start_ts) if data_start_ts else None
    anchor = bool(data_start and data_start >= start)  # 기간 시작이 기록 시작일보다 앞이면 기록 시작일부터
    if anchor:
        start = data_start

    held = [s for s, p in data["positions"].items() if (p.get("qty") or 0) > pnl.EPS]
    symbols = sorted({t["symbol"] for t in trades} | set(held))
    infos = await _symbol_infos(conn, symbols, markets)
    price_since = _shift(start, -PRICE_LOOKBACK_PAD_DAYS)
    price_status: Dict[str, str] = {}
    if fetch_prices and symbols:
        price_status = await daily_prices.ensure_daily_bars(conn, client, infos, price_since, force=refresh)
    closes = await daily_prices.load_closes(conn, infos, price_since)

    close_dates = [d for series in closes.values() for d in series]
    axis = pnl.build_axis(close_dates + [t["date"] for t in trades], start, today)
    opening_qty = {s: o["qty"] for s, o in data["opening"].items()}
    mtm = pnl.daily_mtm(trades, opening_qty, closes, {s: infos[s]["market"] for s in infos}, axis, data["fx"],
                        anchor_first_day=anchor)
    daily = pnl.portfolio_daily(mtm, axis)

    net = {s: 0.0 for s in symbols}
    for t in trades:
        net[t["symbol"]] = net.get(t["symbol"], 0.0) + (t["qty"] if t["side"] == "buy" else -t["qty"])
    # 브로커 실제 잔고(positions)가 있으면 이를 최우선으로 사용한다.
    # 체결 역산값(opening_qty + net)은 잔고 테이블에 기록되지 않은 과거 종목의 폴백으로만 사용한다.
    holdings_end = {
        s: float(data["positions"][s].get("qty", 0.0))
        if s in data["positions"]
        else max(0.0, opening_qty.get(s, 0.0) + net.get(s, 0.0))
        for s in symbols
    }
    last_close = {s: series[max(series)] for s, series in closes.items() if series}
    fx_now = data["fx"](time.time())
    active_mismatch = {
        sym: miss for sym, miss in data["mismatch"].items()
        if not (settings.DOMESTIC_ONLY and infos.get(sym, {}).get("market") == "overseas")
    }
    summary = pnl.summarize_symbols(
        data["events"], trades, data["state"], data["opening"], holdings_end, last_close,
        {s: infos[s]["market"] for s in infos}, mtm, active_mismatch, fx_now,
    )

    matrix = [
        {"symbol": sym, "date": d, "pnl_krw": r["pnl_krw"], "ret": r["ret"], "qty": r["qty"], "close": r["close"]}
        for sym, rows in mtm.items() for d, r in rows.items()
    ]
    equity_points, equity_dropped = pnl.filter_equity_outliers(data["equity_points"])
    equity = [e for e in pnl.equity_by_day(equity_points) if e["date"] >= start]
    realized_total = sum(e["pnl_krw"] for e in data["events"])
    unrealized_total = sum(r["unrealized_krw"] for r in summary)
    wins = sum(1 for e in data["events"] if e["pnl_krw"] > 0)

    warnings: List[str] = []
    if data["unpriced"]:
        warnings.append(f"가격을 알 수 없어 제외한 체결 {len(data['unpriced'])}건")
    for sym, missing in active_mismatch.items():
        warnings.append(f"{sym}: 잔고(positions)보다 체결 매수가 {missing:g}주 많음 — 체결 이력 기준으로 계산")
    if not data["has_fx"] and any(m == "overseas" for m in markets.values()):
        warnings.append("환율 기록이 없어 해외 종목을 1:1로 환산했습니다")
    failed = [s for s, st in price_status.items() if st in ("failed", "stale")]
    if failed:
        warnings.append(f"일봉 조회 실패(또는 캐시 사용): {', '.join(failed)}")
    if equity_dropped:
        warnings.append(f"총자산 기록 {equity_dropped}건은 중앙값의 50% 미만(한쪽 계좌만 조회된 값으로 추정)이라 차트에서 제외")
    missing_close = [s for s in symbols if not closes.get(s)]
    if missing_close:
        warnings.append(f"가격 데이터가 없어 일별 손익에서 제외: {', '.join(missing_close)}")

    return {
        "generated_at": time.time(),
        "range": {"start": start, "end": today, "days": days, "data_start": data_start},
        "kpis": {
            "holdings_value_krw": sum(r["value_krw"] or 0.0 for r in summary),
            "realized_krw": realized_total, "unrealized_krw": unrealized_total,
            "total_pnl_krw": realized_total + unrealized_total,
            "period_pnl_krw": daily[-1]["cum_pnl_krw"] if daily else 0.0,
            "period_return": daily[-1]["cum_ret"] if daily else None,
            "max_drawdown": pnl.max_drawdown(daily) if daily else None,
            "win_rate": (wins / len(data["events"])) if data["events"] else None,
            "sell_count": len(data["events"]), "trade_count": len(trades),
            "equity_krw": equity[-1]["equity_krw"] if equity else None,
            "fx_now": fx_now if data["has_fx"] else None,
        },
        "symbols": _clean_rows(summary),
        "daily": _clean_rows(daily),
        "matrix": _clean_rows(matrix),
        "equity": equity,
        "price_status": price_status,
        "warnings": warnings,
    }


# --- 달력 (실현손익) ---

async def build_realized(
    conn: aiosqlite.Connection, start: str, end: str, symbol: Optional[str] = None
) -> Dict[str, Any]:
    data = await _load_trade_data(conn)
    events = [e for e in data["events"] if start <= e["date"] <= end and (not symbol or e["symbol"] == symbol)]
    trades = [t for t in data["trades"] if start <= t["date"] <= end and (not symbol or t["symbol"] == symbol)]

    days: Dict[str, Dict[str, Any]] = {}
    for e in events:
        d = days.setdefault(e["date"], {"date": e["date"], "pnl_krw": 0.0, "sells": 0, "wins": 0, "buys": 0,
                                         "buy_amount_krw": 0.0, "estimated": False})
        d["pnl_krw"] += e["pnl_krw"]
        d["sells"] += 1
        d["wins"] += 1 if e["pnl_krw"] > 0 else 0
        d["estimated"] = d["estimated"] or e["estimated"]
    for t in trades:
        if t["side"] != "buy":
            continue
        d = days.setdefault(t["date"], {"date": t["date"], "pnl_krw": 0.0, "sells": 0, "wins": 0, "buys": 0,
                                         "buy_amount_krw": 0.0, "estimated": False})
        d["buys"] += 1
        d["buy_amount_krw"] += t["qty"] * t["price"] * t["fx"]

    trade_rows = [
        {"date": t["date"], "ts": t["ts"], "symbol": t["symbol"], "market": t["market"], "side": t["side"],
         "qty": t["qty"], "price": t["price"], "amount_krw": t["qty"] * t["price"] * t["fx"],
         "estimated": t["estimated"], "reason": t["reason"]}
        for t in trades
    ]
    event_by_seq = {e["seq"]: e for e in events}
    for row, t in zip(trade_rows, trades):
        ev = event_by_seq.get(t["seq"]) if t["side"] == "sell" else None
        row["pnl_krw"] = ev["pnl_krw"] if ev else None
    return {
        "range": {"start": start, "end": end},
        "days": _clean_rows(sorted(days.values(), key=lambda x: x["date"])),
        "trades": _clean_rows(trade_rows),
        "month_total_krw": sum(e["pnl_krw"] for e in events),
        "note": "해외 종목은 미국 현지 거래일 기준, 수수료·세금 미반영",
    }


# --- 가격 ---

async def get_prices(
    conn: aiosqlite.Connection, client: Optional[AsyncKISClient], symbol: str, interval: str, days: int
) -> Dict[str, Any]:
    infos = await _symbol_infos(conn, [symbol], {})
    market = infos[symbol]["market"]
    since = _shift(_today(), -max(days, 1))
    if interval == "30m":
        rows = await db.get_cached_intraday_bars(conn, symbol, market, _date_to_ts(since))
        return {"symbol": symbol, "market": market, "interval": "30m", "status": "cached",
                "bars": [{"ts": r["bar_start"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
                         for r in rows]}
    status = await daily_prices.ensure_daily_bars(conn, client, infos, since)
    rows = await db.get_daily_bars(conn, symbol, market, since)
    return {"symbol": symbol, "market": market, "interval": "1d", "status": status.get(symbol),
            "bars": [{"date": r["date"], "close": r["close"]} for r in rows]}


# --- 의사결정 복기 ---

def _marker_kind(row: Dict[str, Any]) -> str:
    if row["action"] == "NO_OP":
        return "noop"
    status = row.get("intent_status")
    if status in ("FILLED", "PARTIALLY_FILLED"):
        return "executed"
    if status in ("REJECTED", "CANCELLED"):
        return "rejected"
    if status in ("PENDING", "SUBMITTED", "UNKNOWN"):
        return "pending"
    return "not_submitted"  # NOT_SUBMITTED 또는 주문 없이 로그만 남은 BUY/SELL


async def list_decisions(
    conn: aiosqlite.Connection, symbol: Optional[str], start: str, end: str, include_noop: bool
) -> List[Dict[str, Any]]:
    rows = await db.list_decisions_light(conn, symbol, _date_to_ts(start), _date_to_ts(end, True), include_noop)
    out = []
    for r in rows:
        market = r.pop("market", None) or "domestic"
        ctx_price = r.pop("ctx_price", None)
        ctx_price_foreign = r.pop("ctx_price_foreign", None)
        r["price"] = r.get("intent_price") or (ctx_price_foreign if market == "overseas" else ctx_price)
        r["market"] = market
        r["date"] = pnl.local_date(market, r["ts"])
        r["marker"] = _marker_kind(r)
        out.append(r)
    return _clean_rows(out)


async def get_decision_detail(conn: aiosqlite.Connection, decision_id: int) -> Optional[Dict[str, Any]]:
    row = await db.get_decision(conn, decision_id)
    if row is None:
        return None
    row["context"] = parse_context(row.pop("context_json", None))
    row["marker"] = _marker_kind(row)
    return row


# --- 리밸런싱 이력 ---

def _loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _event_view(ev: Dict[str, Any]) -> Dict[str, Any]:
    ctx = _loads(ev.get("context_json"), {})
    output = ctx.get("output") or {}
    prior = _loads(ev.get("prior_snapshot_json"), {})
    proposed = _loads(ev.get("proposed_snapshot_json"), None)
    return {
        "id": ev["id"], "trigger_type": ev["trigger_type"], "trigger_detail": ev.get("trigger_detail"),
        "autonomy_mode": ev["autonomy_mode"], "status": ev["status"], "rationale": ev.get("rationale"),
        "symbols_added": _loads(ev.get("symbols_added"), []), "symbols_removed": _loads(ev.get("symbols_removed"), []),
        "prior_weights": prior.get("weights", {}), "proposed_weights": (proposed or {}).get("weights"),
        "llm_output": output, "rejected_adds": ctx.get("rejected_adds", []), "error": ev.get("error"),
        "created_at": ev["created_at"], "decided_at": ev.get("decided_at"), "decided_by": ev.get("decided_by"),
    }


def weighted_return(weights: Dict[str, float], closes: Dict[str, Dict[str, float]], start: str) -> Tuple[Optional[float], List[str]]:
    """start 날짜 종가(없으면 그 이전 마지막 종가) 대비 최신 종가의 비중가중 수익률. 데이터 없는 종목은 제외하고 재정규화."""
    total_w = 0.0
    acc = 0.0
    skipped: List[str] = []
    for sym, w in weights.items():
        series = closes.get(sym) or {}
        before = [d for d in series if d <= start]
        if w <= 0:
            continue
        if not before or not series:
            skipped.append(sym)
            continue
        p0, p1 = series[max(before)], series[max(series)]
        if p0 <= 0:
            skipped.append(sym)
            continue
        acc += w * (p1 / p0 - 1.0)
        total_w += w
    return ((acc / total_w) if total_w > 0 else None), skipped


async def build_rebalance_history(
    conn: aiosqlite.Connection, client: Optional[AsyncKISClient], limit: int = 50, with_returns: bool = False
) -> Dict[str, Any]:
    events = [_event_view(e) for e in await db.list_rebalance_events(conn, limit)]
    applied_raw = await db.list_approved_weight_events(conn)
    applied = [_event_view(e) for e in applied_raw]

    # 승인 비중 타임라인: 최초 이벤트의 직전 상태 -> 각 적용 이벤트 -> 현재 활성 비중
    timeline: List[Dict[str, Any]] = []
    if applied:
        first = applied[0]
        timeline.append({"ts": first["created_at"], "label": "시작", "weights": first["prior_weights"]})
    for ev in applied:
        if ev["proposed_weights"] is not None:
            timeline.append({"ts": ev["decided_at"] or ev["created_at"], "label": f"#{ev['id']} {ev['trigger_type']}",
                             "weights": ev["proposed_weights"]})
    active = await db.get_active_target_weights(conn)
    if active:
        timeline.append({"ts": time.time(), "label": "현재", "weights": active})

    decided = [e for e in events if e["status"] in APPLIED_STATUSES + ("REJECTED",)]
    stats = {
        "by_status": _count(e["status"] for e in events),
        "by_trigger": _count(e["trigger_type"] for e in events),
        "approval_rate": (sum(1 for e in decided if e["status"] in APPLIED_STATUSES) / len(decided)) if decided else None,
        "decided_count": len(decided),
    }

    returns: List[Dict[str, Any]] = []
    if with_returns and applied:
        syms = sorted({s for e in applied for s in list((e["prior_weights"] or {})) + list((e["proposed_weights"] or {}))})
        infos = await _symbol_infos(conn, syms, {})
        since = _shift(datetime.fromtimestamp(min(e["decided_at"] or e["created_at"] for e in applied), KST).strftime("%Y-%m-%d"), -7)
        await daily_prices.ensure_daily_bars(conn, client, infos, since)
        closes = await daily_prices.load_closes(conn, infos, since)
        for e in applied:
            if e["proposed_weights"] is None:
                continue
            decided_day = datetime.fromtimestamp(e["decided_at"] or e["created_at"], KST).strftime("%Y-%m-%d")
            prior_ret, prior_skipped = weighted_return(e["prior_weights"], closes, decided_day)
            new_ret, new_skipped = weighted_return(e["proposed_weights"], closes, decided_day)
            returns.append({
                "event_id": e["id"], "decided_day": decided_day, "prior_return": prior_ret, "proposed_return": new_ret,
                "excess": (new_ret - prior_ret) if prior_ret is not None and new_ret is not None else None,
                "skipped": sorted(set(prior_skipped + new_skipped)),
            })
    return {"events": _clean_rows(events), "timeline": timeline, "stats": stats, "returns": _clean_rows(returns),
            "note": "제안 이후 수익률은 현지통화 종가 기준(환율 미반영), 승인 시점 종가 대비 최신 종가"}


def _count(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out
