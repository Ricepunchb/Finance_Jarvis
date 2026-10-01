# ui/rebalance.py
"""🔁 리밸런싱 이력 — AI 에이전트의 제안 → 승인/거부 흐름, 비중 변화, 제안의 사후 성과."""
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui import theme
from ui.common import KST, cached_get, clear_cache, fmt_kst, fmt_pct, symbol_label

STATUS_BADGE = {
    "APPROVED": "🟢 승인", "AUTO_APPLIED": "🟢 자동적용", "PROPOSED": "🟡 제안(대기)", "REJECTED": "🔴 거부",
    "BLOCKED": "⛔ 차단", "FAILED": "⚠️ 실패", "SKIPPED": "⚪ 건너뜀", "RUNNING": "⏳ 실행중",
}
TRIGGER_LABEL = {"MANUAL": "수동", "SCHEDULED": "정기", "DRIFT": "드리프트", "NEWS_EVENT": "뉴스", "ROLLBACK": "롤백"}
MAX_STACK = 8  # 카테고리 슬롯 상한. 넘으면 나머지는 '기타'


def _stacked_weights(timeline: List[Dict[str, Any]], names: Dict[str, str]) -> go.Figure:
    # 최대 비중이 큰 종목부터 슬롯을 배정한다. 색은 종목에 고정(theme.symbol_color)이라 필터/기간이 바뀌어도 유지된다.
    peak: Dict[str, float] = {}
    for point in timeline:
        for sym, w in point["weights"].items():
            peak[sym] = max(peak.get(sym, 0.0), w)
    ranked = sorted(peak, key=lambda s: -peak[s])
    shown, folded = ranked[:MAX_STACK], ranked[MAX_STACK:]
    x = [datetime.fromtimestamp(p["ts"], KST) for p in timeline]
    surface = theme.tokens()["surface"]
    fig = go.Figure()
    for sym in shown:
        fig.add_trace(go.Scatter(
            x=x, y=[p["weights"].get(sym, 0.0) * 100 for p in timeline], name=symbol_label(sym, names), mode="lines",
            stackgroup="w", line=dict(width=0.5, color=surface), fillcolor=theme.symbol_color(sym),
            line_shape="hv", hovertemplate=symbol_label(sym, names) + " %{y:.1f}%<extra></extra>",
        ))
    if folded:
        fig.add_trace(go.Scatter(
            x=x, y=[sum(p["weights"].get(s, 0.0) for s in folded) * 100 for p in timeline], name="기타", mode="lines",
            stackgroup="w", line=dict(width=0.5, color=surface), fillcolor=theme.OTHER_GRAY, line_shape="hv",
            hovertemplate="기타 %{y:.1f}%<extra></extra>",
        ))
    # 적용 이벤트 위치 (첫 점 '시작'은 제외)
    for xi, p in zip(x[1:], timeline[1:]):
        fig.add_vline(x=xi, line=dict(color=theme.tokens()["muted"], width=1, dash="dot"))
        fig.add_annotation(x=xi, y=1.0, yref="paper", text=p["label"], showarrow=False, yanchor="bottom",
                           font=dict(size=10, color=theme.tokens()["muted"]))
    fig.update_xaxes(tickformat="%m-%d %H:%M")
    fig.update_yaxes(ticksuffix="%", range=[0, 100])
    fig = theme.style(fig, height=380, hovermode="x unified")
    fig.update_layout(legend=dict(y=1.13), margin=dict(t=48))  # 이벤트 라벨과 범례가 겹치지 않게
    return fig


def _dumbbell(prior: Dict[str, float], proposed: Dict[str, float], names: Dict[str, str]) -> go.Figure:
    syms = sorted(set(prior) | set(proposed), key=lambda s: -max(prior.get(s, 0), proposed.get(s, 0)))
    before_c, after_c = theme.BEFORE_AFTER[theme.mode()]
    y = [symbol_label(s, names) for s in syms]
    before = [prior.get(s, 0.0) * 100 for s in syms]
    after = [proposed.get(s, 0.0) * 100 for s in syms]
    fig = go.Figure()
    for yi, b, a in zip(y, before, after):
        fig.add_trace(go.Scatter(x=[b, a], y=[yi, yi], mode="lines", line=dict(color=theme.tokens()["axis"], width=2),
                                 showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=before, y=y, mode="markers", name="이전", marker=dict(size=15, color="rgba(0,0,0,0)", line=dict(color=before_c, width=2.5)),
                             hovertemplate="%{y}<br>이전 %{x:.2f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=after, y=y, mode="markers", name="제안", marker=dict(size=9, color=after_c, line=dict(color=theme.tokens()["surface"], width=1.5)),
                             hovertemplate="%{y}<br>제안 %{x:.2f}%<extra></extra>"))
    fig.update_xaxes(ticksuffix="%", showgrid=True, gridcolor=theme.tokens()["grid"])
    fig.update_yaxes(type="category", autorange="reversed", showgrid=False, zeroline=False)
    return theme.style(fig, height=max(200, 36 * len(syms) + 80))


def _render_event(ev: Dict[str, Any], names: Dict[str, str]) -> None:
    badge = STATUS_BADGE.get(ev["status"], ev["status"])
    title = f"#{ev['id']} · {TRIGGER_LABEL.get(ev['trigger_type'], ev['trigger_type'])} · {badge} · {fmt_kst(ev['created_at'], '%m-%d %H:%M')}"
    with st.expander(title):
        meta = f"모드 {ev['autonomy_mode']}"
        if ev.get("decided_by"):
            meta += f" · 결정 {ev['decided_by']} ({fmt_kst(ev.get('decided_at'))})"
        if ev.get("trigger_detail"):
            meta += f" · 트리거: {ev['trigger_detail']}"
        st.caption(meta)
        if ev.get("rationale"):
            st.markdown(ev["rationale"])
        if ev.get("error"):
            st.error(ev["error"])
        if ev.get("proposed_weights"):
            st.plotly_chart(_dumbbell(ev["prior_weights"], ev["proposed_weights"], names), width="stretch", key=f"db-{ev['id']}")
        for title_, items in (("편입", ev["symbols_added"]), ("제외", ev["symbols_removed"])):
            if items:
                st.markdown(f"**{title_}**")
                st.dataframe(pd.DataFrame([{"종목": symbol_label(i["symbol"], names), "이름": i.get("name", ""), "사유": i.get("rationale", "")} for i in items]),
                             width="stretch", hide_index=True)
        if ev.get("rejected_adds"):
            with st.expander("검증에서 탈락한 편입 제안"):
                st.json(ev["rejected_adds"])
        if ev.get("llm_output"):
            with st.expander("LLM 출력 원본"):
                st.json(ev["llm_output"])


def render(names: Dict[str, str]) -> None:
    returns_key = "rb_with_returns"
    with_returns = st.session_state.get(returns_key, False)
    data = cached_get("/analytics/rebalance-history", ttl=60, timeout=300, limit=100, with_returns=with_returns)
    if data is None:
        st.error("리밸런싱 이력을 불러오지 못했습니다.")
        return
    events, stats = data["events"], data["stats"]
    if not events:
        st.info("아직 리밸런싱 이벤트가 없습니다.")
        return

    by_status = stats["by_status"]
    applied = by_status.get("APPROVED", 0) + by_status.get("AUTO_APPLIED", 0)
    k = st.columns(5)
    k[0].metric("전체 이벤트", f"{len(events)}건")
    k[1].metric("적용", f"{applied}건")
    k[2].metric("거부", f"{by_status.get('REJECTED', 0)}건")
    k[3].metric("차단·실패", f"{by_status.get('BLOCKED', 0) + by_status.get('FAILED', 0)}건")
    k[4].metric("승인율", fmt_pct(stats["approval_rate"], digits=0) if stats["approval_rate"] is not None else "-",
                help=f"결정된 {stats['decided_count']}건 중 적용 비율 (차단·실패·건너뜀 제외)")

    cross: Dict[str, Dict[str, int]] = {}
    for e in events:
        cross.setdefault(TRIGGER_LABEL.get(e["trigger_type"], e["trigger_type"]), {})
        label = STATUS_BADGE.get(e["status"], e["status"])
        cross[TRIGGER_LABEL.get(e["trigger_type"], e["trigger_type"])][label] = cross[TRIGGER_LABEL.get(e["trigger_type"], e["trigger_type"])].get(label, 0) + 1
    st.markdown("##### 트리거 × 결과")
    st.dataframe(pd.DataFrame(cross).T.fillna(0).astype(int), width="stretch")

    timeline = data["timeline"]
    if len(timeline) >= 2:
        st.markdown("##### 목표 비중 변화 (적용된 이벤트 기준)")
        st.plotly_chart(_stacked_weights(timeline, names), width="stretch")
        st.caption("수동 비중 승인(리밸런싱 이벤트에 묶이지 않은 변경)은 반영되지 않고, 마지막 '현재'만 실제 활성 비중입니다.")

    st.markdown("##### 제안 이후 성과")
    if not with_returns:
        st.caption("승인 시점의 종가부터 최신 종가까지, 이전 포트폴리오와 제안 포트폴리오의 비중가중 수익률을 비교합니다. "
                   "KIS 일봉을 새로 조회하므로 시간이 걸릴 수 있습니다.")
        if st.button("📈 제안 이후 수익률 계산", key="rb_calc"):
            st.session_state[returns_key] = True
            clear_cache()
            st.rerun()
    else:
        rets = [r for r in data["returns"] if r["excess"] is not None]
        if not rets:
            st.caption("계산 가능한 이벤트가 없습니다(가격 데이터 부족).")
        else:
            c = theme.gain_loss()
            fig = go.Figure(go.Bar(
                x=[f"#{r['event_id']} ({r['decided_day']})" for r in rets], y=[r["excess"] * 100 for r in rets],
                marker=dict(color=[c["gain"] if r["excess"] >= 0 else c["loss"] for r in rets]),
                hovertemplate="%{x}<br>제안−이전 %{y:+.2f}%p<extra></extra>",
            ))
            fig.update_layout(bargap=0.5)
            fig.update_xaxes(type="category")
            fig.update_yaxes(ticksuffix="%p")
            st.plotly_chart(theme.style(fig, height=260, legend=False), width="stretch")
            st.dataframe(pd.DataFrame([{
                "이벤트": f"#{r['event_id']}", "승인일": r["decided_day"],
                "이전 포트": r["prior_return"] * 100 if r["prior_return"] is not None else None,
                "제안 포트": r["proposed_return"] * 100 if r["proposed_return"] is not None else None,
                "초과(%p)": r["excess"] * 100, "가격 없음": ", ".join(r["skipped"]),
            } for r in rets]), width="stretch", hide_index=True, column_config={
                "이전 포트": st.column_config.NumberColumn(format="%+.2f%%"),
                "제안 포트": st.column_config.NumberColumn(format="%+.2f%%"),
                "초과(%p)": st.column_config.NumberColumn(format="%+.2f"),
            })
            st.caption(data["note"] + " · 이벤트 간 기간이 겹쳐 서로 독립적인 성과가 아닙니다.")
        if st.button("끄기", key="rb_off"):
            st.session_state[returns_key] = False
            st.rerun()

    st.markdown("##### 이벤트 목록")
    for ev in events:
        _render_event(ev, names)
