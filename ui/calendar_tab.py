# ui/calendar_tab.py
"""📅 매매 달력 — 일별 실현손익을 달력으로 보고, 날짜를 누르면 그날의 체결을 본다."""
import calendar as pycal
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st
from streamlit_calendar import calendar

from ui import theme
from ui.common import api_get, cached_get, fmt_krw, fmt_kst, symbol_label, REASON_LABELS, today_kst


def _month_options(data_start: Optional[str]) -> List[str]:
    today = date.fromisoformat(today_kst())
    start = date.fromisoformat(data_start) if data_start else today
    months, y, m = [], today.year, today.month
    while (y, m) >= (start.year, start.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return months


def _events(days: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    c = theme.gain_loss()
    t = theme.tokens()
    events = []
    for d in days:
        if d["sells"]:
            win = d["pnl_krw"] >= 0
            mark = "≈" if d["estimated"] else ""
            events.append({
                "title": f"{mark}{d['pnl_krw']:+,.0f}", "start": d["date"], "allDay": True,
                "backgroundColor": c["gain"] if win else c["loss"], "borderColor": c["gain"] if win else c["loss"],
                "textColor": "#ffffff",
            })
        if d["buys"]:
            events.append({
                "title": f"매수 {d['buys']}건", "start": d["date"], "allDay": True,
                "backgroundColor": "transparent", "borderColor": t["muted"], "textColor": t["text2"],
            })
    return events


def _clicked_date(state: Dict[str, Any]) -> Optional[str]:
    click = state.get("dateClick")
    if click:
        return click.get("dateStr") or (click.get("date") or "")[:10] or None
    ev = state.get("eventClick")
    if ev:
        start = (ev.get("event") or {}).get("start") or ""
        return start[:10] or None
    return None


def render(data_start: Optional[str], symbols: List[str], names: Dict[str, str]) -> None:
    months = _month_options(data_start)
    # 기본 월: 체결이 있었던 가장 최근 달 (이번 달이 비어 있으면 빈 달력부터 보여주지 않는다)
    overall = cached_get("/analytics/realized", ttl=120, timeout=60, start=data_start or months[-1] + "-01", end=today_kst()) or {}
    active_days = [d["date"] for d in overall.get("days", [])]
    default_month = active_days[-1][:7] if active_days and active_days[-1][:7] in months else months[0]
    c1, c2 = st.columns([1, 2])
    month = c1.selectbox("월", months, index=months.index(default_month), key="cal_month")
    symbol = c2.selectbox("종목", ["전체"] + symbols, key="cal_symbol",
                          format_func=lambda s: s if s == "전체" else symbol_label(s, names))

    y, m = int(month[:4]), int(month[5:])
    start, end = f"{month}-01", f"{month}-{pycal.monthrange(y, m)[1]:02d}"
    params = {"start": start, "end": end}
    if symbol != "전체":
        params["symbol"] = symbol
    data = api_get("/analytics/realized", timeout=60, **params)
    if data is None:
        st.error("실현손익 데이터를 불러오지 못했습니다.")
        return

    days = data["days"]
    sells = sum(d["sells"] for d in days)
    wins = sum(d["wins"] for d in days)
    k = st.columns(4)
    k[0].metric("월 실현손익", fmt_krw(data["month_total_krw"], signed=True))
    k[1].metric("청산 건수", f"{sells}건")
    k[2].metric("승률", f"{wins / sells * 100:.0f}%" if sells else "-")
    k[3].metric("매수 건수", f"{sum(d['buys'] for d in days)}건")

    state = calendar(
        events=_events(days),
        options={
            "initialView": "dayGridMonth", "initialDate": start, "locale": "ko", "height": 560,
            "headerToolbar": {"left": "", "center": "title", "right": ""},
            "dayMaxEvents": 3, "fixedWeekCount": False,
        },
        callbacks=["dateClick", "eventClick"],
        key=f"cal-{month}-{symbol}",
    ) or {}
    clicked = _clicked_date(state)
    if clicked and clicked in {d["date"] for d in days}:
        st.session_state["cal_day"] = clicked
    st.caption("≈ 표시는 체결 기록이 없어 추정한 손익입니다. 손익은 매도(청산) 시점에 확정되고, 매수만 있는 날은 건수만 표시합니다. "
               f"{data['note']}.")

    day_options = [d["date"] for d in days]
    if not day_options:
        st.info("이 달에는 체결 기록이 없습니다.")
        return
    default = st.session_state.get("cal_day")
    idx = day_options.index(default) if default in day_options else len(day_options) - 1
    picked = st.selectbox("날짜 상세", day_options, index=idx, key=f"cal_pick-{month}-{symbol}",
                          help="달력에서 날짜나 손익을 클릭해도 선택됩니다.")

    st.markdown(f"#### {picked} 체결")
    trades = [t for t in data["trades"] if t["date"] == picked]
    st.dataframe(pd.DataFrame([{
        "시각(KST)": fmt_kst(t["ts"]), "종목": symbol_label(t["symbol"], names), "구분": "매수" if t["side"] == "buy" else "매도",
        "수량": t["qty"], "체결가(현지)": t["price"], "금액(원)": t["amount_krw"], "실현손익(원)": t["pnl_krw"],
        "사유": REASON_LABELS.get(t["reason"], t["reason"] or "-"), "비고": "추정" if t["estimated"] else "",
    } for t in trades]), width="stretch", hide_index=True, column_config={
        "수량": st.column_config.NumberColumn(format="%g"),
        "체결가(현지)": st.column_config.NumberColumn(format="%,.2f"),
        "금액(원)": st.column_config.NumberColumn(format="%,.0f"),
        "실현손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
    })

    st.markdown("#### 종목별 월간 합계")
    by_symbol: Dict[str, Dict[str, float]] = {}
    for t in data["trades"]:
        row = by_symbol.setdefault(t["symbol"], {"buys": 0, "sells": 0, "pnl": 0.0, "buy_amt": 0.0})
        if t["side"] == "buy":
            row["buys"] += 1
            row["buy_amt"] += t["amount_krw"]
        else:
            row["sells"] += 1
            row["pnl"] += t["pnl_krw"] or 0.0
    st.dataframe(pd.DataFrame([{
        "종목": symbol_label(s, names), "매수(건)": r["buys"], "매수금액(원)": r["buy_amt"], "청산(건)": r["sells"], "실현손익(원)": r["pnl"],
    } for s, r in sorted(by_symbol.items(), key=lambda kv: -kv[1]["pnl"])]), width="stretch", hide_index=True, column_config={
        "매수금액(원)": st.column_config.NumberColumn(format="%,.0f"),
        "실현손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
    })
