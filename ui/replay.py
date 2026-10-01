# ui/replay.py
"""🧠 의사결정 복기 — 가격 차트 위에 엔진의 BUY/SELL 판단을 겹쳐 보고, 각 판단의 근거(시그널·비중·원본 스냅샷)를 펼쳐 본다."""
import bisect
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui import theme
from ui.common import (
    KST, MARKER_LABELS, REASON_LABELS, STATUS_LABELS, cached_get, fmt_kst, fmt_krw, fmt_pct, symbol_label, today_kst,
)

SIGNAL_LABELS = {"tech_signal": "기술(일봉)", "intraday_signal": "장중(30분봉)", "sentiment_signal": "뉴스 감성", "valuation_signal": "밸류에이션"}


def _bars_axis(prices: Dict[str, Any]) -> Dict[str, Any]:
    bars = prices.get("bars") or []
    if prices.get("interval") == "30m":
        starts = [b["ts"] for b in bars]
        labels = [datetime.fromtimestamp(b["ts"], KST).strftime("%m-%d %H:%M") for b in bars]
    else:
        starts = [b["date"] for b in bars]
        labels = list(starts)
    return {"keys": starts, "labels": labels, "close": [b["close"] for b in bars]}


def _bucket(axis: Dict[str, Any], interval: str, decision: Dict[str, Any]) -> Optional[int]:
    keys = axis["keys"]
    if not keys:
        return None
    target = decision["ts"] if interval == "30m" else decision["date"]
    i = bisect.bisect_right(keys, target) - 1
    return i if i >= 0 else None


def _price_chart(prices: Dict[str, Any], decisions: List[Dict[str, Any]], show_failed: bool, show_noop: bool):
    interval = prices.get("interval", "1d")
    axis = _bars_axis(prices)
    t = theme.tokens()
    palette = theme.CATEGORICAL[theme.mode()]
    buy_color, sell_color = palette[2], palette[1]
    currency = "USD" if prices.get("market") == "overseas" else "원"

    fig = go.Figure(go.Scatter(
        x=axis["labels"], y=axis["close"], mode="lines", name="종가" if interval == "1d" else "30분봉 종가",
        line=dict(color=theme.accent(), width=2), hovertemplate="%{x}<br>%{y:,.2f}" + currency + "<extra></extra>",
    ))
    groups: Dict[str, Dict[str, list]] = {k: {"x": [], "y": [], "text": []} for k in ("buy", "sell", "failed", "noop")}
    skipped = 0
    for d in decisions:
        idx = _bucket(axis, interval, d)
        if idx is None:
            skipped += 1
            continue
        if d["marker"] == "executed":
            key = "buy" if d["action"] == "BUY" else "sell"
        elif d["marker"] == "noop":
            if not show_noop:
                continue
            key = "noop"
        else:
            if not show_failed:
                continue
            key = "failed"
        price = d.get("price") or axis["close"][idx]
        g = groups[key]
        g["x"].append(axis["labels"][idx])
        g["y"].append(price)
        status = STATUS_LABELS.get(d.get("intent_status") or "", MARKER_LABELS.get(d["marker"], ""))
        g["text"].append(
            f"{fmt_kst(d['ts'], '%m-%d %H:%M')} · {d['action']} {d.get('qty') or 0:g}주 @ {price:,.2f}"
            f"<br>{status} · {REASON_LABELS.get(d.get('reason') or '', (d.get('reason') or '-')[:40])}"
            f"<br>비중 {fmt_pct(d.get('current_weight'))} → 목표 {fmt_pct(d.get('target_weight'))}"
        )
    specs = [
        ("buy", "매수(체결)", "triangle-up", buy_color, 12),
        ("sell", "매도(체결)", "triangle-down", sell_color, 12),
        ("failed", "거부·미전송·미체결", "x", t["muted"], 9),
        ("noop", "관망", "circle", t["axis"], 5),
    ]
    for key, name, symbol, color, size in specs:
        g = groups[key]
        if not g["x"]:
            continue
        fig.add_trace(go.Scatter(
            x=g["x"], y=g["y"], mode="markers", name=name, text=g["text"], hovertemplate="%{text}<extra></extra>",
            marker=dict(symbol=symbol, size=size, color=color, line=dict(color=t["surface"], width=2)),
        ))
    fig.update_xaxes(type="category", nticks=12, tickangle=-45)
    fig.update_yaxes(tickformat=",.2f" if currency == "USD" else ",.0f", title=dict(text=f"가격({currency})", font=dict(size=11)))
    return theme.style(fig, height=420), skipped


def _signal_table(ctx: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for key, label in SIGNAL_LABELS.items():
        s = ctx.get(key)
        if isinstance(s, dict):
            rows.append({"신호": label, "방향": s.get("direction", "-"), "강도": s.get("strength"), "상세": str(s.get("detail", ""))})
    return pd.DataFrame(rows)


def _render_detail(detail: Dict[str, Any], names: Dict[str, str]) -> None:
    ctx = detail.get("context") or {}
    status = STATUS_LABELS.get(detail.get("intent_status") or "", "주문 없음")
    st.markdown(
        f"**{fmt_kst(detail['ts'], '%Y-%m-%d %H:%M')} · {symbol_label(detail['symbol'], names)} · {detail['action']}** "
        f"— {status} · {REASON_LABELS.get(detail.get('reason') or '', detail.get('reason') or '-')}"
    )
    m = st.columns(4)
    m[0].metric("현재 비중", fmt_pct(detail.get("current_weight")))
    m[1].metric("목표 비중", fmt_pct(detail.get("target_weight")))
    m[2].metric("드리프트", fmt_pct(detail.get("drift"), signed=True))
    m[3].metric("수량", f"{detail.get('qty') or 0:g}주")
    price = ctx.get("price") or ctx.get("price_foreign")
    if price:
        extra = f" · 환율 {ctx['bass_exrt']:,.1f}" if ctx.get("bass_exrt") else ""
        st.caption(f"판단 시점 가격 {price:,.2f}{extra} · 총자산 {fmt_krw(ctx.get('total_equity'))}")

    sig = _signal_table(ctx)
    if not sig.empty:
        st.markdown("##### 시그널")
        st.dataframe(sig, width="stretch", hide_index=True, column_config={"강도": st.column_config.NumberColumn(format="%.2f")})
    if detail.get("reason") and detail["action"] == "NO_OP":
        st.caption(f"관망 사유: {detail['reason']}")

    checks = {k: ctx[k] for k in ("exit_check", "action_before_recheck", "buyable_check", "sellable_check", "qty_after_recheck") if ctx.get(k) is not None}
    if checks:
        st.markdown("##### 청산·주문 점검")
        st.json(checks, expanded=True)
    with st.expander("원본 스냅샷(context_json)"):
        st.json(ctx)


def render(symbols: List[str], data_start: Optional[str], names: Dict[str, str]) -> None:
    today = date.fromisoformat(today_kst())
    floor = date.fromisoformat(data_start) if data_start else today - timedelta(days=30)
    c1, c2, c3 = st.columns([2, 2, 2])
    rng = c1.date_input("기간", value=(max(floor, today - timedelta(days=14)), today), min_value=floor, max_value=today, key="rp_range")
    if not isinstance(rng, tuple) or len(rng) != 2:
        st.info("시작일과 종료일을 모두 선택하세요.")
        return
    start, end = rng[0].isoformat(), rng[1].isoformat()

    active = cached_get("/analytics/decisions", ttl=30, start=start, end=end, include_noop=False) or []
    options = sorted({d["symbol"] for d in active} | set(symbols))
    if not options:
        st.info("복기할 결정 기록이 없습니다.")
        return
    symbol = c2.selectbox("종목", options, key="rp_symbol", format_func=lambda s: symbol_label(s, names))
    interval_label = c3.radio("봉", ["일봉", "30분봉"], horizontal=True, key="rp_interval")
    t1, t2, t3 = st.columns(3)
    show_failed = t1.toggle("거부·미전송 표시", value=True, key="rp_failed")
    show_noop = t2.toggle("관망(NO_OP) 표시", value=False, key="rp_noop")
    only_orders = t3.toggle("표에는 주문만", value=True, key="rp_only_orders")

    decisions = cached_get("/analytics/decisions", ttl=30, start=start, end=end, symbol=symbol, include_noop=True) or []
    interval = "1d" if interval_label == "일봉" else "30m"
    days = (today - date.fromisoformat(start)).days + 5
    prices = cached_get("/analytics/prices", ttl=120, symbol=symbol, interval=interval, days=days)
    if not prices or not prices.get("bars"):
        st.warning("이 구간의 가격 데이터가 없습니다. (30분봉은 엔진이 돈 날만 캐시에 있습니다. 일봉은 오버뷰의 '데이터 새로고침'으로 받을 수 있습니다.)")
        return
    if prices.get("status") in ("failed", "stale", "fallback_intraday"):
        st.caption("⚠️ KIS 일봉 조회에 실패해 30분봉 캐시 또는 기존 캐시로 대체했습니다. 엔진이 돌지 않은 날은 비어 있을 수 있습니다.")

    fig, skipped = _price_chart(prices, decisions, show_failed, show_noop)
    st.plotly_chart(fig, width="stretch")
    if skipped:
        st.caption(f"가격 데이터 범위 밖이라 차트에 표시하지 못한 결정 {skipped}건")

    rows = [d for d in decisions if (d["marker"] != "noop" or not only_orders)]
    st.markdown(f"##### 결정 목록 ({len(rows)}건) — 행을 선택하면 근거를 봅니다")
    if not rows:
        st.caption("이 기간에는 표시할 결정이 없습니다.")
        return
    rows = list(reversed(rows))
    table = pd.DataFrame([{
        "시각": fmt_kst(d["ts"], "%m-%d %H:%M"), "액션": {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "NO_OP": "⚪ 관망"}.get(d["action"], d["action"]),
        "상태": STATUS_LABELS.get(d.get("intent_status") or "", MARKER_LABELS.get(d["marker"], "")),
        "수량": d.get("qty"), "가격": d.get("price"), "현재비중": (d.get("current_weight") or 0) * 100,
        "목표비중": (d.get("target_weight") or 0) * 100, "기술": d.get("tech_signal") or "-",
        "사유": REASON_LABELS.get(d.get("reason") or "", (d.get("reason") or "-")[:60]),
    } for d in rows])
    event = st.dataframe(
        table, width="stretch", hide_index=True, height=300, on_select="rerun", selection_mode="single-row", key=f"rp_table-{symbol}",
        column_config={
            "수량": st.column_config.NumberColumn(format="%g"), "가격": st.column_config.NumberColumn(format="%,.2f"),
            "현재비중": st.column_config.NumberColumn(format="%.2f%%"), "목표비중": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    picked = event.selection.rows if event and event.selection else []
    if not picked:
        st.caption("👆 표에서 행을 선택하세요.")
        return
    detail = cached_get(f"/analytics/decisions/{rows[picked[0]]['id']}", ttl=300)
    if detail is None:
        st.error("결정 상세를 불러오지 못했습니다.")
        return
    st.divider()
    _render_detail(detail, names)
