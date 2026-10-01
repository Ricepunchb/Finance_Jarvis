# ui/overview.py
"""📊 성과 오버뷰 — 같은 수익률을 종목축 / 날짜축 / 종목×날짜 히트맵 세 가지로 본다."""
import math
from typing import Any, Dict, List

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui import theme
from ui.common import fmt_krw, fmt_pct, symbol_label

VIEWS = ["종목별", "날짜별", "종목×날짜"]


def _kpis(k: Dict[str, Any]) -> None:
    r1 = st.columns(4)
    r1[0].metric("보유 평가액", fmt_krw(k["holdings_value_krw"]), help="보유 수량 × 최신 종가(해외는 환율 적용)")
    r1[1].metric("누적 손익", fmt_krw(k["total_pnl_krw"], signed=True), help="실현손익 + 평가손익 (원가 기준)")
    r1[2].metric("실현 손익", fmt_krw(k["realized_krw"], signed=True), help="매도 시점 확정 손익 (이동평균단가)")
    r1[3].metric("평가 손익", fmt_krw(k["unrealized_krw"], signed=True), help="현재 보유분의 미실현 손익")
    r2 = st.columns(4)
    r2[0].metric("기간 손익", fmt_krw(k["period_pnl_krw"], signed=True),
                 help="선택 기간의 일별 평가손익 합 (환차손익 포함)")
    r2[1].metric("기간 수익률", fmt_pct(k["period_return"], signed=True), help="일수익률을 이어 붙인 값")
    r2[2].metric("최대 낙폭(MDD)", fmt_pct(k["max_drawdown"]), help="기간 내 고점 대비 최대 하락")
    win = k["win_rate"]
    r2[3].metric("매도 승률", fmt_pct(win, digits=0) if win is not None else "-", help=f"청산 {k['sell_count']}건 기준")


def _bar_by_symbol(rows: List[Dict[str, Any]], names: Dict[str, str], metric: str) -> go.Figure:
    colors = theme.gain_loss()
    key, is_pct = {"누적손익(원)": ("total_pnl_krw", False), "수익률(ROI)": ("roi", True), "기간손익(원)": ("period_pnl_krw", False)}[metric]
    data = sorted((r for r in rows if r.get(key) is not None), key=lambda r: r[key])
    values = [r[key] * (100 if is_pct else 1) for r in data]
    fig = go.Figure(go.Bar(
        x=values, y=[symbol_label(r["symbol"], names) for r in data], orientation="h",
        marker=dict(color=[colors["gain"] if v >= 0 else colors["loss"] for v in values]),
        text=[f"{v:+.2f}%" if is_pct else f"{v:+,.0f}" for v in values], textposition="outside", cliponaxis=False,
        hovertemplate="%{y}<br>" + ("%{x:+.2f}%" if is_pct else "%{x:+,.0f}원") + "<extra></extra>",
    ))
    fig.update_layout(bargap=0.45)
    fig.update_xaxes(ticksuffix="%" if is_pct else "", tickformat=None if is_pct else ",.0f")
    # 종목코드("000660")를 Plotly가 숫자 축으로 오인하지 않도록 카테고리로 고정
    fig.update_yaxes(type="category", showgrid=False, zeroline=False)
    fig.update_xaxes(showgrid=True, gridcolor=theme.tokens()["grid"], zeroline=True, zerolinecolor=theme.tokens()["axis"])
    return theme.style(fig, height=max(220, 44 * len(data) + 60), legend=False)


def _symbol_table(rows: List[Dict[str, Any]], names: Dict[str, str]) -> pd.DataFrame:
    out = []
    for r in rows:
        notes = []
        if r["estimated"]:
            notes.append("추정 포함")
        if r.get("mismatch_qty"):
            notes.append(f"잔고 불일치({r['mismatch_qty']:g}주)")
        out.append({
            "종목": symbol_label(r["symbol"], names), "시장": "해외(USD)" if r["market"] == "overseas" else "국내",
            "수량": r["qty"], "평단(현지)": r["avg_local"], "종가(현지)": r["last_close"], "평가액(원)": r["value_krw"],
            "실현손익(원)": r["realized_krw"], "평가손익(원)": r["unrealized_krw"], "누적손익(원)": r["total_pnl_krw"],
            "ROI": (r["roi"] * 100) if r["roi"] is not None else None, "기간손익(원)": r["period_pnl_krw"],
            "승률": (r["win_rate"] * 100) if r["win_rate"] is not None else None,
            "거래": r["trade_count"], "비고": ", ".join(notes),
        })
    return pd.DataFrame(out)


def _render_by_symbol(data: Dict[str, Any], names: Dict[str, str]) -> None:
    rows = data["symbols"]
    if not rows:
        st.caption("아직 체결 이력이나 보유 종목이 없습니다.")
        return
    metric = st.radio("지표", ["누적손익(원)", "수익률(ROI)", "기간손익(원)"], horizontal=True, key="ov_symbol_metric")
    st.plotly_chart(_bar_by_symbol(rows, names, metric), width="stretch")
    st.dataframe(
        _symbol_table(rows, names), width="stretch", hide_index=True,
        column_config={
            "수량": st.column_config.NumberColumn(format="%.0f"),
            "평단(현지)": st.column_config.NumberColumn(format="%,.2f"),
            "종가(현지)": st.column_config.NumberColumn(format="%,.2f"),
            "평가액(원)": st.column_config.NumberColumn(format="%,.0f"),
            "실현손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
            "평가손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
            "누적손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
            "ROI": st.column_config.NumberColumn(format="%+.2f%%"),
            "기간손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
            "승률": st.column_config.NumberColumn(format="%.0f%%"),
        },
    )
    st.caption("누적손익은 매수 원가 기준(실현+평가), 기간손익은 선택 기간의 일별 평가손익 합이라 두 값은 다를 수 있습니다.")


def _render_by_date(data: Dict[str, Any]) -> None:
    daily = data["daily"]
    if not daily:
        st.caption("선택 기간에 표시할 일별 데이터가 없습니다.")
        return
    colors = theme.gain_loss()
    dates = [d["date"] for d in daily]

    st.markdown("##### 일별 평가손익")
    pnl = [d["pnl_krw"] for d in daily]
    fig = go.Figure(go.Bar(
        x=dates, y=pnl, marker=dict(color=[colors["gain"] if v >= 0 else colors["loss"] for v in pnl]),
        hovertemplate="%{x}<br>%{y:+,.0f}원<extra></extra>",
    ))
    fig.update_layout(bargap=0.4)
    fig.update_xaxes(type="category", tickangle=-45)
    fig.update_yaxes(tickformat=",.0f", ticksuffix="")
    st.plotly_chart(theme.style(fig, height=280, legend=False), width="stretch")

    st.markdown("##### 누적 수익률과 낙폭")
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(
        x=dates, y=[d["cum_ret"] * 100 for d in daily], mode="lines", name="누적 수익률", line=dict(color=theme.tokens()["text2"], width=2),
        hovertemplate="%{x}<br>누적 %{y:+.2f}%<extra></extra>",
    ))
    fig2.add_trace(go.Scatter(
        x=dates, y=[d["drawdown"] * 100 for d in daily], mode="lines", name="고점 대비 낙폭",
        line=dict(color=theme.tokens()["muted"], width=1.5, dash="dot"),
        hovertemplate="%{x}<br>낙폭 %{y:.2f}%<extra></extra>",
    ))
    fig2.update_xaxes(type="category", tickangle=-45)
    fig2.update_yaxes(ticksuffix="%")
    st.plotly_chart(theme.style(fig2, height=300, hovermode="x unified"), width="stretch")

    equity = data.get("equity") or []
    if len(equity) >= 2:
        with st.expander("계좌 총자산 추이 (엔진 기록, 참고용)"):
            fig3 = go.Figure(go.Scatter(
                x=[e["date"] for e in equity], y=[e["equity_krw"] for e in equity], mode="lines+markers", name="총자산",
                line=dict(color=theme.tokens()["text2"], width=2), marker=dict(size=6),
                hovertemplate="%{x}<br>%{y:,.0f}원<extra></extra>",
            ))
            fig3.update_xaxes(type="category")
            fig3.update_yaxes(tickformat=",.0f", rangemode="normal")
            st.plotly_chart(theme.style(fig3, height=240, legend=False), width="stretch")
            st.caption("입출금은 추적되지 않아 입출금이 있으면 손익과 다를 수 있습니다.")

    with st.expander("표로 보기"):
        st.dataframe(pd.DataFrame([{
            "날짜": d["date"], "평가손익(원)": d["pnl_krw"], "일수익률(%)": (d["ret"] * 100) if d["ret"] is not None else None,
            "누적손익(원)": d["cum_pnl_krw"], "누적수익률(%)": d["cum_ret"] * 100, "낙폭(%)": d["drawdown"] * 100,
        } for d in daily]), width="stretch", hide_index=True, column_config={
            "평가손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
            "일수익률(%)": st.column_config.NumberColumn(format="%+.2f"),
            "누적손익(원)": st.column_config.NumberColumn(format="%+,.0f"),
            "누적수익률(%)": st.column_config.NumberColumn(format="%+.2f"),
            "낙폭(%)": st.column_config.NumberColumn(format="%.2f"),
        })


def _render_heatmap(data: Dict[str, Any], names: Dict[str, str]) -> None:
    matrix = data["matrix"]
    if not matrix:
        st.caption("선택 기간에 표시할 종목×날짜 데이터가 없습니다.")
        return
    metric = st.radio("값", ["일수익률(%)", "평가손익(원)"], horizontal=True, key="ov_heat_metric")
    use_ret = metric.startswith("일수익률")
    df = pd.DataFrame(matrix)
    df["val"] = df["ret"] * 100 if use_ret else df["pnl_krw"]
    # 보유가 없던 날(수량 0, 손익 0)은 빈 칸으로 둔다
    df.loc[(df["qty"].abs() < 1e-9) & (df["pnl_krw"].abs() < 1e-9), "val"] = float("nan")
    dates = sorted(df["date"].unique())
    order = df.groupby("symbol")["pnl_krw"].sum().sort_values(ascending=False).index.tolist()
    z = df.pivot(index="symbol", columns="date", values="val").reindex(index=order, columns=dates)
    pnl_p = df.pivot(index="symbol", columns="date", values="pnl_krw").reindex(index=order, columns=dates)
    close_p = df.pivot(index="symbol", columns="date", values="close").reindex(index=order, columns=dates)
    qty_p = df.pivot(index="symbol", columns="date", values="qty").reindex(index=order, columns=dates)

    finite = [abs(v) for v in z.to_numpy().flatten() if isinstance(v, float) and not math.isnan(v)]
    bound = max(finite) if finite else 1.0
    labels = [symbol_label(s, names) for s in order]
    custom = [[[pnl_p.iloc[i, j], close_p.iloc[i, j], qty_p.iloc[i, j]] for j in range(len(dates))] for i in range(len(order))]
    show_text = len(order) * len(dates) <= 120
    fmt = "{:+.1f}" if use_ret else "{:+,.0f}"
    text = [[("" if (isinstance(v, float) and math.isnan(v)) else fmt.format(v)) for v in row] for row in z.to_numpy()] if show_text else None
    fig = go.Figure(go.Heatmap(
        z=z.to_numpy(), x=dates, y=labels, zmid=0, zmin=-bound, zmax=bound, colorscale=theme.diverging_colorscale(),
        xgap=2, ygap=2, customdata=custom, text=text, texttemplate="%{text}" if show_text else None,
        textfont=dict(size=11), hoverongaps=False,
        colorbar=dict(title=dict(text="%" if use_ret else "원"), thickness=10, len=0.8),
        hovertemplate="%{y}<br>%{x}<br>" + ("수익률 %{z:+.2f}%" if use_ret else "손익 %{z:+,.0f}원")
                      + "<br>손익 %{customdata[0]:+,.0f}원 · 종가 %{customdata[1]:,.2f} · 보유 %{customdata[2]:g}주<extra></extra>",
    ))
    fig.update_xaxes(type="category", tickangle=-45, side="bottom")
    fig.update_yaxes(type="category", autorange="reversed", showgrid=False, zeroline=False)
    st.plotly_chart(theme.style(fig, height=max(240, 46 * len(order) + 120), legend=False), width="stretch")
    st.caption("행은 기간 손익 큰 순, 색은 0을 중립(회색)으로 한 양/음 대칭 척도입니다. 빈 칸은 보유하지 않은 날입니다.")

    totals = df.groupby("symbol")["pnl_krw"].sum().reindex(order)
    with st.expander("종목별 기간 합계 · 표로 보기"):
        st.dataframe(pd.DataFrame({"종목": labels, "기간 평가손익(원)": totals.to_numpy()}), width="stretch", hide_index=True,
                     column_config={"기간 평가손익(원)": st.column_config.NumberColumn(format="%+,.0f")})


def render(data: Dict[str, Any], names: Dict[str, str]) -> None:
    for w in data.get("warnings", []):
        st.warning(w, icon="⚠️")
    _kpis(data["kpis"])
    rng = data["range"]
    st.caption(
        f"기간 {rng['start']} ~ {rng['end']} (엔진 기록 시작 {rng.get('data_start') or '-'}) · 수수료·세금 미반영 · "
        "해외 종목은 미국 현지 거래일·원화 환산 · 추정치는 '추정 포함'으로 표시"
    )
    view = st.segmented_control("보기", VIEWS, default=VIEWS[0], key="ov_view") or VIEWS[0]
    if view == "종목별":
        _render_by_symbol(data, names)
    elif view == "날짜별":
        _render_by_date(data)
    else:
        _render_heatmap(data, names)
