# core/pnl.py
"""매매 성과 계산 (순수 함수 — DB/네트워크 접근 없음, 단위 테스트 대상).

데이터 한계에 맞춘 설계:
- fills에는 심볼/side/통화/수수료가 없어 order_intents와 합쳐서 쓴다. 수수료·세금은 반영하지 않는다(총손익 기준).
- fills.filled_at은 "기록 시각"(체결 약 30분 뒤)이라 날짜 버킷은 order_intents.created_at 기준.
- FILLED인데 fills가 없는 주문(과거 REST 체결 기록 이전)은 decision_log 컨텍스트 가격으로 추정(estimated=True).
- 보유수량은 현재 positions에서 체결 이력을 거꾸로 되감아 복원한다(조회 이전 매수분이 fills에 없기 때문).
  단 positions가 체결 이력과 모순되면(예: 해외 잔고가 0인데 매수 체결이 있음) 체결 기준으로 계산하고 mismatch로 표시한다.
- 거래일 기준: 국내는 KST, 해외는 미국 현지일(America/New_York). 일봉 날짜와 일치시키고 자정 걸친 세션이 두 날로 갈리지 않게 한다.
- 모든 금액은 KRW 환산값(`*_krw`)과 원통화(`*_local`)를 함께 보관한다.
"""
import bisect
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9))
NEW_YORK = ZoneInfo("America/New_York")
EPS = 1e-9


# --- 시간/환율 ---

def local_date(market: str, ts: float) -> str:
    """거래소 현지 거래일 'YYYY-MM-DD'. 국내 KST, 해외 미국 현지일."""
    tz = NEW_YORK if market == "overseas" else KST
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def kst_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d")


def eod_ts(date_str: str) -> float:
    """해당 날짜(KST 라벨) 23:59:59의 epoch. 일 단위 환율 조회용."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    return (d + timedelta(days=1) - timedelta(seconds=1)).timestamp()


def parse_context(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def extract_points(context_rows: Iterable[Dict[str, Any]]) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """decision_log 행들에서 (환율 시계열, 총자산 시계열)을 뽑는다. 각각 [(ts, value)] 시간순."""
    fx_points: List[Tuple[float, float]] = []
    equity_points: List[Tuple[float, float]] = []
    for row in context_rows:
        ctx = parse_context(row.get("context_json"))
        ts = row["ts"]
        rate = ctx.get("bass_exrt")
        if isinstance(rate, (int, float)) and rate > 0:
            fx_points.append((ts, float(rate)))
        equity = ctx.get("total_equity")
        if isinstance(equity, (int, float)) and equity > 0:
            equity_points.append((ts, float(equity)))
    fx_points.sort()
    equity_points.sort()
    return fx_points, equity_points


def make_fx(points: List[Tuple[float, float]]) -> Callable[[float], float]:
    """시각 -> USDKRW. 가장 가까운 기록값을 쓰고, 기록이 전혀 없으면 1.0(호출부가 has_fx로 경고)."""
    pts = sorted(points)
    times = [p[0] for p in pts]

    def fx(ts: float) -> float:
        if not pts:
            return 1.0
        i = bisect.bisect_left(times, ts)
        if i == 0:
            return pts[0][1]
        if i == len(pts):
            return pts[-1][1]
        before, after = pts[i - 1], pts[i]
        return before[1] if ts - before[0] <= after[0] - ts else after[1]

    return fx


def fx_for(market: str, ts: float, fx: Callable[[float], float]) -> float:
    return fx(ts) if market == "overseas" else 1.0


# --- 체결 -> 거래 ---

def build_trades(
    intents: List[Dict[str, Any]],
    fills: List[Dict[str, Any]],
    decision_ctx_by_intent: Dict[str, Dict[str, Any]],
    fx: Callable[[float], float],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """체결(fills) + fills가 없는 FILLED 주문의 추정 체결을 거래 목록으로 만든다.

    반환: (trades 시간순, 가격을 알 수 없어 제외한 intent_id 목록).
    추정 매도는 decision_log의 exit_check 평단을 원가 힌트(basis_hint_*)로 함께 싣는다.
    """
    fills_by_intent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in fills:
        fills_by_intent[f["intent_id"]].append(f)

    trades: List[Dict[str, Any]] = []
    unpriced: List[str] = []
    for it in sorted(intents, key=lambda x: x["created_at"]):
        market = it.get("market") or "domestic"
        ts = it["created_at"]
        trade_fx = fx_for(market, ts, fx)
        base = {
            "ts": ts, "date": local_date(market, ts), "symbol": it["symbol"], "market": market,
            "side": it["side"], "intent_id": it["intent_id"], "reason": it.get("reason"),
            "fx": trade_fx, "basis_hint_local": None, "basis_hint_krw": None,
        }
        own_fills = fills_by_intent.get(it["intent_id"])
        if own_fills:
            for f in own_fills:
                trades.append({**base, "qty": float(f["qty"]), "price": float(f["price"]), "estimated": False})
            continue
        if it["status"] != "FILLED":
            continue  # SUBMITTED/REJECTED/NOT_SUBMITTED 등은 체결이 아니다
        ctx = decision_ctx_by_intent.get(it["intent_id"], {})
        price = it.get("price")
        if not price:
            price = ctx.get("price_foreign") if market == "overseas" else ctx.get("price")
        if not price:
            unpriced.append(it["intent_id"])
            continue
        trade = {**base, "qty": float(it["qty"]), "price": float(price), "estimated": True}
        if it["side"] == "sell":
            exit_check = ctx.get("exit_check") or {}
            if market == "overseas":
                hint_krw = exit_check.get("avg_price_krw")
                if hint_krw:
                    trade["basis_hint_krw"] = float(hint_krw)
                    trade["basis_hint_local"] = float(hint_krw) / trade_fx
            elif exit_check.get("avg_price"):
                trade["basis_hint_krw"] = trade["basis_hint_local"] = float(exit_check["avg_price"])
        trades.append(trade)
    for seq, t in enumerate(trades):
        t["seq"] = seq  # 이벤트/거래 매핑용 순번 (부분 체결로 intent_id가 겹쳐도 구분)
    return trades, unpriced


def _signed(trade: Dict[str, Any]) -> float:
    return trade["qty"] if trade["side"] == "buy" else -trade["qty"]


# --- 보유수량 복원 / 오프닝 ---

def compute_opening(
    trades: List[Dict[str, Any]], positions: Dict[str, Dict[str, Any]], fx: Callable[[float], float]
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, float]]:
    """조회 구간 시작 시점의 보유(오프닝) 추정.

    opening_qty = 현재 positions 수량 - 체결 순증감. 음수면 positions가 체결 이력과 모순된 것이므로
    오프닝을 0으로 두고 mismatch[symbol] = 모자란 수량(양수)로 기록한다.
    오프닝 평단: 매도가 없었다면 positions 평단에서 체결 매수분을 빼서 역산, 아니면 entry_avg_price/평단.
    반환: (opening{sym:{qty,avg_local,avg_krw}}, mismatch{sym: missing_qty}).
    """
    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_symbol[t["symbol"]].append(t)

    opening: Dict[str, Dict[str, Any]] = {}
    mismatch: Dict[str, float] = {}
    for sym in set(by_symbol) | set(positions):
        sym_trades = by_symbol.get(sym, [])
        pos = positions.get(sym) or {}
        cur_qty = float(pos.get("qty") or 0.0)
        net = sum(_signed(t) for t in sym_trades)
        raw_open = cur_qty - net
        if raw_open < -EPS:
            mismatch[sym] = -raw_open
            opening[sym] = {"qty": 0.0, "avg_local": 0.0, "avg_krw": 0.0}
            continue
        if raw_open <= EPS:
            opening[sym] = {"qty": 0.0, "avg_local": 0.0, "avg_krw": 0.0}
            continue
        market = pos.get("market") or (sym_trades[0]["market"] if sym_trades else "domestic")
        avg_now = float(pos.get("avg_price") or 0.0)
        has_sell = any(t["side"] == "sell" for t in sym_trades)
        buy_cost = sum(t["qty"] * t["price"] for t in sym_trades if t["side"] == "buy")
        implied = (avg_now * cur_qty - buy_cost) / raw_open if not has_sell and avg_now > 0 else 0.0
        avg_local = implied if implied > 0 else float(pos.get("entry_avg_price") or avg_now or 0.0)
        first_ts = sym_trades[0]["ts"] if sym_trades else float(pos.get("last_synced_at") or 0.0)
        opening[sym] = {"qty": raw_open, "avg_local": avg_local, "avg_krw": avg_local * fx_for(market, first_ts, fx)}
    return opening, mismatch


# --- 실현손익 (이동평균단가법) ---

def realized_events(
    trades: List[Dict[str, Any]], opening: Dict[str, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, float]]]:
    """종목별 이동평균단가로 매도 시점의 실현손익을 계산한다.

    원가 우선순위: 추정 매도의 exit_check 평단(엔진이 그 시점에 쓴 값) > 이동평균단가.
    보유가 모자란 매도(조회 이전 매수분)는 estimated로 표시한다.
    반환: (events, 종료 시점 상태{sym:{qty,avg_local,avg_krw}}).
    """
    state: Dict[str, Dict[str, float]] = {
        sym: {"qty": o["qty"], "avg_local": o["avg_local"], "avg_krw": o["avg_krw"]} for sym, o in opening.items()
    }
    events: List[Dict[str, Any]] = []
    for t in trades:
        s = state.setdefault(t["symbol"], {"qty": 0.0, "avg_local": 0.0, "avg_krw": 0.0})
        q, p, fx = t["qty"], t["price"], t["fx"]
        if t["side"] == "buy":
            new_qty = s["qty"] + q
            s["avg_local"] = (s["qty"] * s["avg_local"] + q * p) / new_qty
            s["avg_krw"] = (s["qty"] * s["avg_krw"] + q * p * fx) / new_qty
            s["qty"] = new_qty
            continue
        estimated = t["estimated"]
        if t["basis_hint_krw"] is not None:
            cost_local, cost_krw = t["basis_hint_local"], t["basis_hint_krw"]
        elif s["qty"] > EPS:
            cost_local, cost_krw = s["avg_local"], s["avg_krw"]
            if s["qty"] + EPS < q:
                estimated = True
        else:
            cost_local, cost_krw = p, p * fx  # 원가를 알 수 없음 -> 손익 0으로 두고 추정 표시
            estimated = True
        events.append({
            "ts": t["ts"], "date": t["date"], "symbol": t["symbol"], "market": t["market"],
            "qty": q, "price": p, "cost_local": cost_local,
            "pnl_local": (p - cost_local) * q, "pnl_krw": (p * fx - cost_krw) * q,
            "estimated": estimated, "reason": t.get("reason"), "intent_id": t["intent_id"], "seq": t.get("seq"),
        })
        s["qty"] = max(0.0, s["qty"] - q)
    return events, state


# --- 일별 평가손익 (종목 x 날짜) ---

def build_axis(dates: Iterable[str], start: str, end: str) -> List[str]:
    return sorted({d for d in dates if start <= d <= end})


def _ffill(closes: Dict[str, float], axis: List[str]) -> Tuple[Dict[str, float], Optional[float]]:
    """축 위로 종가를 앞쪽 채움한다. 축 이전 마지막 종가(기준가)를 함께 반환."""
    known = sorted(closes)
    before = [d for d in known if d < axis[0]]
    prev_close = closes[before[-1]] if before else None
    out: Dict[str, float] = {}
    last = prev_close
    for d in axis:
        if d in closes:
            last = closes[d]
        out[d] = last if last is not None else None
    # 축 앞쪽 공백은 첫 종가로 채운다(그 구간엔 보유 변동이 없다고 본다)
    first = next((v for v in out.values() if v is not None), None)
    for d in axis:
        if out[d] is None:
            out[d] = first
    return out, (prev_close if prev_close is not None else first)


def daily_mtm(
    trades: List[Dict[str, Any]],
    opening_qty: Dict[str, float],
    closes: Dict[str, Dict[str, float]],
    markets: Dict[str, str],
    axis: List[str],
    fx: Callable[[float], float],
    anchor_first_day: bool = False,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """종목 x 날짜 평가손익.  pnl = 평가액 변화 + 현금흐름(−매수대금/+매도대금), KRW 기준(환차손익 포함).

    축은 거래일을 모두 포함해야 한다(거래가 축 밖이면 현금흐름이 누락된다).
    anchor_first_day: 첫 날의 기준가를 그날 종가로 둔다 — 데이터 기록 시작일에는 그 이전 가격 변동을
    (엔진이 보유하지 않았을 수 있으므로) 손익에 넣지 않기 위함.
    일수익률 = pnl / (전일 평가액 + 당일 매수액). 분모가 사실상 0이면 None.
    """
    if not axis:
        return {}
    trades_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        trades_by_symbol[t["symbol"]].append(t)

    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for sym in set(trades_by_symbol) | {s for s, q in opening_qty.items() if q > EPS}:
        series = closes.get(sym) or {}
        if not series:
            continue
        market = markets.get(sym, "domestic")
        filled, prev_close = _ffill(series, axis)
        qty = opening_qty.get(sym, 0.0) + sum(_signed(t) for t in trades_by_symbol.get(sym, []) if t["date"] < axis[0])
        if anchor_first_day:
            prev_close = filled[axis[0]]
        fx_prev = fx_for(market, eod_ts(axis[0]) - (0 if anchor_first_day else 86400), fx)
        value_prev = qty * prev_close * fx_prev
        by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in trades_by_symbol.get(sym, []):
            if t["date"] >= axis[0]:
                by_date[t["date"]].append(t)

        rows: Dict[str, Dict[str, Any]] = {}
        for d in axis:
            day_trades = by_date.get(d, [])
            cash_krw = cash_local = buys_krw = 0.0
            for t in day_trades:
                amount_local = t["qty"] * t["price"]
                sign = -1.0 if t["side"] == "buy" else 1.0
                cash_local += sign * amount_local
                cash_krw += sign * amount_local * t["fx"]
                if t["side"] == "buy":
                    buys_krw += amount_local * t["fx"]
                qty += _signed(t)
            fx_d = fx_for(market, eod_ts(d), fx)
            close = filled[d]
            value_krw = qty * close * fx_d
            pnl_krw = value_krw - value_prev + cash_krw
            denom = value_prev + buys_krw
            rows[d] = {
                "qty": qty, "close": close, "value_krw": value_krw, "pnl_krw": pnl_krw,
                "base_krw": denom, "buys_krw": buys_krw, "cash_krw": cash_krw,
                "ret": (pnl_krw / denom) if denom > 1.0 else None,
            }
            value_prev = value_krw
        result[sym] = rows
    return result


def portfolio_daily(mtm: Dict[str, Dict[str, Dict[str, Any]]], axis: List[str]) -> List[Dict[str, Any]]:
    """종목별 일손익을 합쳐 포트폴리오 일별 시리즈(손익, 수익률, 누적수익률 지수, MDD)를 만든다."""
    out: List[Dict[str, Any]] = []
    index = 1.0
    peak = 1.0
    cum_pnl = 0.0
    for d in axis:
        pnl = sum(rows[d]["pnl_krw"] for rows in mtm.values() if d in rows)
        base = sum(rows[d]["base_krw"] for rows in mtm.values() if d in rows)
        value = sum(rows[d]["value_krw"] for rows in mtm.values() if d in rows)
        ret = (pnl / base) if base > 1.0 else None
        if ret is not None:
            index *= 1.0 + ret
        peak = max(peak, index)
        cum_pnl += pnl
        out.append({
            "date": d, "pnl_krw": pnl, "value_krw": value, "ret": ret, "cum_pnl_krw": cum_pnl,
            "cum_ret": index - 1.0, "drawdown": index / peak - 1.0,
        })
    return out


def filter_equity_outliers(points: List[Tuple[float, float]]) -> Tuple[List[Tuple[float, float]], int]:
    """총자산 기록 중 전체 중앙값의 50% 미만/200% 초과인 값을 제외한다.

    엔진이 한쪽 계좌(예: 국내만)만 조회했을 때 총자산이 일시적으로 크게 낮게 기록되는 경우가 있어
    그대로 그리면 차트가 톱니 모양이 된다. 반환: (남은 점, 제외한 개수).
    """
    if len(points) < 3:
        return list(points), 0
    values = sorted(v for _, v in points)
    median = values[len(values) // 2]
    kept = [(ts, v) for ts, v in points if 0.5 * median <= v <= 2.0 * median]
    return kept, len(points) - len(kept)


def equity_by_day(equity_points: List[Tuple[float, float]]) -> List[Dict[str, Any]]:
    """계좌 총자산(KRW)을 KST 일자별 마지막 값으로. 입출금은 추적되지 않으므로 참고용."""
    last: Dict[str, float] = {}
    for ts, value in equity_points:
        last[kst_date(ts)] = value
    return [{"date": d, "equity_krw": v} for d, v in sorted(last.items())]


# --- 종목별 요약 ---

def summarize_symbols(
    events: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    state: Dict[str, Dict[str, float]],
    opening: Dict[str, Dict[str, Any]],
    holdings_end: Dict[str, float],
    last_close: Dict[str, float],
    markets: Dict[str, str],
    mtm: Dict[str, Dict[str, Dict[str, Any]]],
    mismatch: Dict[str, float],
    fx_now: float,
) -> List[Dict[str, Any]]:
    """종목축 요약. 누적손익(원가 기준: 실현+평가)과 기간손익(일별 평가손익 합)을 함께 낸다."""
    events_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in events:
        events_by_symbol[e["symbol"]].append(e)
    trades_by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        trades_by_symbol[t["symbol"]].append(t)

    rows: List[Dict[str, Any]] = []
    for sym in sorted(set(trades_by_symbol) | {s for s, o in opening.items() if o["qty"] > EPS}):
        market = markets.get(sym, "domestic")
        evs = events_by_symbol.get(sym, [])
        sym_trades = trades_by_symbol.get(sym, [])
        st = state.get(sym, {"qty": 0.0, "avg_local": 0.0, "avg_krw": 0.0})
        qty = holdings_end.get(sym, 0.0)
        fx = fx_now if market == "overseas" else 1.0
        close = last_close.get(sym)
        value_krw = qty * close * fx if close is not None else None
        cost_krw = qty * st["avg_krw"]
        unrealized_krw = (value_krw - cost_krw) if value_krw is not None and qty > EPS else 0.0
        realized_krw = sum(e["pnl_krw"] for e in evs)
        op = opening.get(sym, {"qty": 0.0, "avg_krw": 0.0})
        invested_krw = op["qty"] * op["avg_krw"] + sum(
            t["qty"] * t["price"] * t["fx"] for t in sym_trades if t["side"] == "buy"
        )
        total = realized_krw + unrealized_krw
        wins = sum(1 for e in evs if e["pnl_krw"] > 0)
        sym_mtm = mtm.get(sym, {})
        rows.append({
            "symbol": sym, "market": market, "qty": qty, "avg_local": st["avg_local"],
            "last_close": close, "value_krw": value_krw,
            "realized_krw": realized_krw, "unrealized_krw": unrealized_krw, "total_pnl_krw": total,
            "invested_krw": invested_krw, "roi": (total / invested_krw) if invested_krw > 1.0 else None,
            "period_pnl_krw": sum(r["pnl_krw"] for r in sym_mtm.values()),
            "trade_count": len(sym_trades), "sell_count": len(evs),
            "win_rate": (wins / len(evs)) if evs else None,
            "estimated": any(e["estimated"] for e in evs) or any(t["estimated"] for t in sym_trades),
            "mismatch_qty": mismatch.get(sym),
        })
    return rows


def max_drawdown(daily: List[Dict[str, Any]]) -> float:
    return min((d["drawdown"] for d in daily), default=0.0)
