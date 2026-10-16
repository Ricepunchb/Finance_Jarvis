# pages/3_미국장_모니터링.py
"""🇺🇸 미국장 시그널 룸 — 30분 주기 모니터링 & 매수/매도 시그널 알림 전용 대시보드.

증권사 API 연결이 불가능한 해외 직투 계좌(달러 투자)를 위해
30분봉·일봉 기술적 지표, 월가 컨센서스, 보유자 맞춤형 손절/트레일링 익절을
실시간 추적하고 알림(화면/사운드/텔레그램)을 제공합니다.
"""
from __future__ import annotations

import ast
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st

from ui.common import API_BASE, api_delete, api_get, api_post, fmt_kst, fmt_pct

st.set_page_config(layout="wide", page_title="🇺🇸 미국장 시그널 룸", page_icon="🇺🇸")

# --- 커스텀 스타일 ---
st.html(
    """
    <style>
    .block-container { padding-top: 1.8rem; max-width: 1350px; }
    div[data-testid="stMetric"] {
        background: rgba(127,127,127,0.06);
        border: 1px solid rgba(127,127,127,0.18);
        border-radius: 12px;
        padding: 12px 16px 8px 16px;
    }
    .signal-card {
        background: rgba(127,127,127,0.05);
        border: 1px solid rgba(127,127,127,0.18);
        border-radius: 14px;
        padding: 18px 20px;
        margin-bottom: 14px;
        transition: border-color 0.15s ease;
    }
    .signal-card:hover {
        border-color: rgba(66, 133, 244, 0.45);
    }
    .badge {
        display: inline-block; padding: 3px 10px; border-radius: 999px;
        font-size: 0.78rem; font-weight: 600; margin-right: 6px; color: white;
    }
    .badge-buy-strong { background: #059669; }
    .badge-buy { background: #10b981; }
    .badge-sell-strong { background: #dc2626; }
    .badge-sell { background: #ef4444; }
    .badge-hold { background: #6b7280; }
    .badge-exit-stop { background: #b91c1c; animation: pulse 2s infinite; }
    .badge-exit-tp { background: #d97706; }
    .badge-mkt-open { background: #10b981; }
    .badge-mkt-pre { background: #f59e0b; }
    .badge-mkt-closed { background: #4b5563; }
    .recommendation-box {
        background: rgba(16, 185, 129, 0.08);
        border-left: 4px solid #10b981;
        border-radius: 6px;
        padding: 10px 14px;
        margin-top: 10px;
        font-size: 0.92rem;
        font-weight: 500;
    }
    .recommendation-box-sell {
        background: rgba(239, 68, 68, 0.08);
        border-left: 4px solid #ef4444;
        border-radius: 6px;
        padding: 10px 14px;
        margin-top: 10px;
        font-size: 0.92rem;
        font-weight: 500;
    }
    .recommendation-box-neutral {
        background: rgba(107, 114, 128, 0.08);
        border-left: 4px solid #6b7280;
        border-radius: 6px;
        padding: 10px 14px;
        margin-top: 10px;
        font-size: 0.92rem;
    }
    </style>
    """
)

# --- 상단 및 사이드바 제어 ---
mkt_status = api_get("/us-market/status", timeout=10)
if mkt_status is None:
    st.error(
        "제어 플레인(FastAPI)에 연결할 수 없습니다. "
        "`uv run uvicorn api.main:app --port 8800` 이 실행 중인지 확인하세요."
    )
    st.stop()

with st.sidebar:
    st.title("🇺🇸 미국장 시그널 룸")
    st.caption("30분 주기 모니터링 & 시그널 관제")
    st.divider()

    # 시장 상태 표시
    status_label = mkt_status.get("status_label", "확인 불가")
    status_code = mkt_status.get("status", "CLOSED")
    badge_class = "badge-mkt-open" if status_code == "REGULAR" else ("badge-mkt-pre" if "PRE" in status_code or "AFTER" in status_code else "badge-mkt-closed")
    st.html(f'<span class="badge {badge_class}">{status_label}</span>')
    st.caption(f"🕒 뉴욕: `{mkt_status.get('ny_time_str', '-')}`")
    st.caption(f"🇰🇷 한국: `{mkt_status.get('kst_time_str', '-')}`")
    st.caption(f"정규장: `{mkt_status.get('kst_reg_hours', '-')}`")

    st.divider()
    st.markdown("##### ⚙️ 30분 스케줄러 & 새로고침")

    sched_running = mkt_status.get("scheduler_running", False)
    sched_toggle = st.toggle("30분 백그라운드 자동 스캔", value=sched_running, key="sidebar_sched_toggle")
    if sched_toggle != sched_running:
        api_post(f"/us-market/scheduler/toggle?enable={str(sched_toggle).lower()}")
        st.toast(f"백그라운드 스케줄러: {'활성화' if sched_toggle else '비활성화'}")
        st.rerun()

    auto_refresh = st.toggle("화면 자동 새로고침", value=False, key="sidebar_auto_refresh")
    refresh_sec = st.slider("새로고침 주기(초)", 10, 300, 30, disabled=not auto_refresh, key="sidebar_refresh_sec")

    if st.button("⚡ 지금 즉시 전체 스캔", width="stretch", type="primary"):
        with st.spinner("미국 모니터링 종목 30분 시그널 전체 스캔 중..."):
            res = api_post("/us-market/scan", {"send_notifications": True}, timeout=60)
            if res and res.get("status") == "ok":
                st.toast(f"✅ 스캔 완료: {res.get('scanned_count')}개 종목 갱신됨")
                time.sleep(0.5)
                st.rerun()

    if st.button("🔄 화면 새로고침", width="stretch"):
        st.rerun()

# 페이지 헤더
col_h1, col_h2 = st.columns([3, 1])
with col_h1:
    st.title("🇺🇸 미국장 시그널 룸")
    st.caption("해외 직투 계좌 전용 — 30분봉 스윙 시그널 & 손절·트레일링 익절 실시간 알림")
with col_h2:
    st.write("")
    last_scan = mkt_status.get("scheduler_last_scan_at")
    scan_cnt = mkt_status.get("scheduler_last_scan_count", 0)
    if last_scan:
        st.info(f"최근 스캔: **{fmt_kst(last_scan)}** ({scan_cnt}종목)")
    else:
        st.caption("최근 자동 스캔 기록 없음")

# 탭 구성
tab_board, tab_manage, tab_history, tab_settings = st.tabs(
    ["📊 30분 실시간 시그널 보드", "💼 관심 & 보유 종목 관리", "🧾 시그널 타임라인", "🔔 알림 & 환경 설정"]
)


# =========================================================================
# TAB 1: 📊 30분 실시간 시그널 보드
# =========================================================================
with tab_board:
    watchlist = api_get("/us-market/watchlist", timeout=15) or []

    if not watchlist:
        st.info(
            "📌 현재 등록된 미국장 모니터링 종목이 없습니다.\n\n"
            "**[💼 관심 & 보유 종목 관리]** 탭에서 종목(예: AAPL, NVDA, TSLA 등)을 추가하거나 "
            "원클릭 프리셋으로 주요 빅테크 종목들을 간편하게 등록해 보세요!"
        )
    else:
        # 요약 메트릭 계산
        total_symbols = len(watchlist)
        buy_count = 0
        sell_count = 0
        exit_alert_count = 0
        total_pnl_usd = 0.0
        has_holdings = False

        alerts_to_show = []

        for item in watchlist:
            sig = item.get("latest_signal") or {}
            action = sig.get("combined_action", "HOLD")
            exit_trig = sig.get("exit_trigger", "NONE")
            price = sig.get("price") or 0.0
            avg_p = item.get("avg_entry_price")
            qty = item.get("held_qty") or 0.0

            if action == "BUY":
                buy_count += 1
            elif action == "SELL":
                sell_count += 1

            if exit_trig in ("STOP_LOSS", "TRAILING_TAKE_PROFIT"):
                exit_alert_count += 1
                alerts_to_show.append((item["symbol"], sig.get("reason", "")))

            if avg_p and avg_p > 0 and qty > 0 and price > 0:
                has_holdings = True
                total_pnl_usd += (price - avg_p) * qty

        # 긴급 알림 배너 (손절 / 트레일링 익절)
        if alerts_to_show:
            for sym, alert_reason in alerts_to_show:
                st.error(f"🚨 **[{sym}]** {alert_reason}")

        # 메트릭 행
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("모니터링 종목 수", f"{total_symbols}개")
        m2.metric("매수 시그널 (BUY)", f"{buy_count}개", delta="진입/비중확대 고려" if buy_count > 0 else None)
        m3.metric(
            "매도/경고 (SELL/Exit)",
            f"{sell_count + exit_alert_count}개",
            delta=f"손절/익절 {exit_alert_count}건" if exit_alert_count > 0 else None,
            delta_color="inverse",
        )
        if has_holdings:
            m4.metric("보유 종목 평가손익(합산)", f"${total_pnl_usd:+,.2f}")
        else:
            m4.metric("시장 상태", status_label)

        st.write("")

        # 필터링 라디오
        filter_col1, filter_col2 = st.columns([2, 1])
        with filter_col1:
            filter_action = st.radio(
                "시그널 필터",
                ["전체 보기", "🟢 매수(BUY)", "🔴 매도/경고(SELL)", "🟡 관망(HOLD)"],
                horizontal=True,
                label_visibility="collapsed",
            )

        st.divider()

        # 종목별 카드 렌더링
        for item in watchlist:
            sym = item["symbol"]
            name = item.get("name") or sym
            sector = item.get("sector") or "US Equity"
            avg_entry = item.get("avg_entry_price")
            held_qty = item.get("held_qty") or 0.0
            sig = item.get("latest_signal") or {}

            action = sig.get("combined_action", "HOLD")
            strength = sig.get("strength") or 0.0
            price = sig.get("price") or 0.0
            change_pct = sig.get("change_pct")
            exit_trigger = sig.get("exit_trigger", "NONE")
            reason = sig.get("reason") or "아직 스캔되지 않음 ('⚡ 지금 즉시 전체 스캔' 클릭)"
            scanned_ts = sig.get("ts")

            # 필터 적용
            if filter_action == "🟢 매수(BUY)" and action != "BUY":
                continue
            if filter_action == "🔴 매도/경고(SELL)" and action != "SELL" and exit_trigger == "NONE":
                continue
            if filter_action == "🟡 관망(HOLD)" and action != "HOLD":
                continue

            # 뱃지 결정
            if exit_trigger == "STOP_LOSS":
                action_badge = '<span class="badge badge-exit-stop">🚨 긴급 손절선 도달</span>'
                rec_box_class = "recommendation-box-sell"
            elif exit_trigger == "TRAILING_TAKE_PROFIT":
                action_badge = '<span class="badge badge-exit-tp">🎯 트레일링 분할익절</span>'
                rec_box_class = "recommendation-box-sell"
            elif action == "BUY":
                action_badge = f'<span class="badge {"badge-buy-strong" if strength >= 0.6 else "badge-buy"}">🟢 BUY ({int(strength*100)}%)</span>'
                rec_box_class = "recommendation-box"
            elif action == "SELL":
                action_badge = f'<span class="badge {"badge-sell-strong" if strength >= 0.6 else "badge-sell"}">🔴 SELL ({int(strength*100)}%)</span>'
                rec_box_class = "recommendation-box-sell"
            else:
                action_badge = '<span class="badge badge-hold">🟡 HOLD (관망)</span>'
                rec_box_class = "recommendation-box-neutral"

            # 보유 손익 계산
            holding_str = ""
            if avg_entry and avg_entry > 0 and price > 0:
                pnl_pct = ((price - avg_entry) / avg_entry) * 100
                pnl_usd = (price - avg_entry) * held_qty
                pnl_color = "#10b981" if pnl_pct >= 0 else "#ef4444"
                holding_str = (
                    f"&nbsp;&nbsp;•&nbsp;&nbsp;내 평단: <b>${avg_entry:,.2f}</b> ({held_qty:.1f}주) "
                    f"| 손익: <b style='color:{pnl_color}'>{pnl_pct:+.2f}% (${pnl_usd:+,.2f})</b>"
                )

            # 가격 등락률 스타일
            chg_str = f"{change_pct:+.2f}%" if change_pct is not None else "-"
            chg_color = "#10b981" if (change_pct or 0) >= 0 else "#ef4444"

            card_html = (
                f'<div class="signal-card">'
                f'<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;">'
                f'<div>'
                f'<span style="font-size:1.35rem; font-weight:700; margin-right:8px;">{sym}</span>'
                f'<span style="font-size:1.0rem; color:gray; margin-right:12px;">{name}</span>'
                f'<span style="font-size:0.8rem; background:rgba(127,127,127,0.15); padding:2px 8px; border-radius:6px;">{sector}</span>'
                f'</div>'
                f'<div style="text-align:right;">'
                f'<span style="font-size:1.35rem; font-weight:700; margin-right:8px;">${price:,.2f}</span>'
                f'<span style="font-size:1.05rem; font-weight:600; color:{chg_color};">{chg_str}</span>'
                f'</div>'
                f'</div>'
                f'<div style="margin-top:8px; font-size:0.88rem;">'
                f'{action_badge}'
                f'<span style="color:gray;">판정시각: {fmt_kst(scanned_ts)}</span>'
                f'{holding_str}'
                f'</div>'
                f'<div class="{rec_box_class}">'
                f'💡 {reason}'
                f'</div>'
                f'</div>'
            )
            st.html(card_html)

            # 개별 종목 인터랙티브 차트 및 상세 분석 펼치기
            with st.expander(f"📈 {sym} 30분봉 차트 & 퀀트 지표 세부 분석", expanded=False):
                chart_data = api_get(f"/us-market/chart/{sym}", timeout=15)
                if chart_data and chart_data.get("bars_30m"):
                    bars = chart_data["bars_30m"]
                    df_chart = pd.DataFrame(bars)
                    df_chart["close"] = pd.to_numeric(df_chart["close"], errors="coerce")
                    df_chart["open"] = pd.to_numeric(df_chart["open"], errors="coerce")
                    df_chart["high"] = pd.to_numeric(df_chart["high"], errors="coerce")
                    df_chart["low"] = pd.to_numeric(df_chart["low"], errors="coerce")
                    df_chart["volume"] = pd.to_numeric(df_chart["volume"], errors="coerce")

                    # 볼린저 밴드 & RSI 계산
                    if len(df_chart) >= 20:
                        try:
                            df_chart.ta.bbands(length=20, std=2, append=True)
                            df_chart.ta.rsi(length=14, append=True)
                        except Exception:
                            pass

                    # 서브플롯 차트 생성
                    fig = make_subplots(
                        rows=2,
                        cols=1,
                        shared_xaxes=True,
                        vertical_spacing=0.08,
                        row_heights=[0.72, 0.28],
                    )

                    # 1. 30분봉 캔들스틱
                    fig.add_trace(
                        go.Candlestick(
                            x=df_chart["date"],
                            open=df_chart["open"],
                            high=df_chart["high"],
                            low=df_chart["low"],
                            close=df_chart["close"],
                            name="30분봉",
                            increasing_line_color="#10b981",
                            decreasing_line_color="#ef4444",
                        ),
                        row=1,
                        col=1,
                    )

                    # 볼린저 밴드 라인
                    bbu_col = [c for c in df_chart.columns if c.startswith("BBU_")]
                    bbl_col = [c for c in df_chart.columns if c.startswith("BBL_")]
                    if bbu_col and bbl_col:
                        fig.add_trace(
                            go.Scatter(
                                x=df_chart["date"],
                                y=df_chart[bbu_col[0]],
                                line=dict(color="rgba(150, 150, 255, 0.5)", width=1, dash="dot"),
                                name="BB 상단",
                            ),
                            row=1,
                            col=1,
                        )
                        fig.add_trace(
                            go.Scatter(
                                x=df_chart["date"],
                                y=df_chart[bbl_col[0]],
                                line=dict(color="rgba(150, 150, 255, 0.5)", width=1, dash="dot"),
                                name="BB 하단",
                            ),
                            row=1,
                            col=1,
                        )

                    # 2. RSI (14)
                    rsi_col = [c for c in df_chart.columns if c.startswith("RSI_")]
                    if rsi_col:
                        fig.add_trace(
                            go.Scatter(
                                x=df_chart["date"],
                                y=df_chart[rsi_col[0]],
                                line=dict(color="#f59e0b", width=1.5),
                                name="RSI (14)",
                            ),
                            row=2,
                            col=1,
                        )
                        fig.add_hline(y=70, line_dash="dash", line_color="rgba(239, 68, 68, 0.5)", row=2, col=1)
                        fig.add_hline(y=30, line_dash="dash", line_color="rgba(16, 185, 129, 0.5)", row=2, col=1)

                    fig.update_layout(
                        height=420,
                        margin=dict(l=20, r=20, t=25, b=20),
                        xaxis_rangeslider_visible=False,
                        template="plotly_dark",
                        showlegend=False,
                    )
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.caption("차트 데이터를 불러오는 중이거나 데이터가 부족합니다.")

                # 세부 지표 스냅샷 파싱
                ind_json_str = sig.get("indicators_json")
                if ind_json_str:
                    try:
                        snap_obj = json.loads(ind_json_str) if isinstance(ind_json_str, str) else ind_json_str
                        intra_snap = snap_obj.get("intraday") or {}
                        cons_snap = snap_obj.get("consensus") or {}

                        c_a, c_b, c_c, c_d = st.columns(4)
                        c_a.metric("30분봉 RSI (14)", f"{intra_snap.get('rsi', '-')}")
                        c_b.metric("30분봉 MACD", f"{intra_snap.get('macd', '-')}")
                        c_c.metric(
                            "월가 평균 목표가",
                            f"${cons_snap.get('target_mean', 0):,.2f}" if cons_snap.get("target_mean") else "-",
                            delta=f"괴리율 {cons_snap.get('target_gap_pct'):+.1f}%" if cons_snap.get("target_gap_pct") is not None else None,
                        )
                        c_d.metric("월가 투자의견", f"{cons_snap.get('recommendation_key', '-').upper()}")
                    except Exception:
                        pass


# =========================================================================
# TAB 2: 💼 관심 & 보유 종목 관리
# =========================================================================
with tab_manage:
    st.subheader("💼 모니터링 종목 & 보유 포지션 등록")
    st.caption("관심 종목을 등록하거나, 실제 보유 중인 평단가($)와 수량을 입력하여 손절·익절 감시를 켜세요.")

    # 원클릭 프리셋 버튼
    st.markdown("##### 🚀 원클릭 테마 프리셋 일괄 추가")
    p_col1, p_col2, p_col3 = st.columns(3)

    PRESET_GROUPS = {
        "bigtech": [("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"), ("GOOGL", "Alphabet"), ("AMZN", "Amazon"), ("META", "Meta"), ("TSLA", "Tesla")],
        "ai_semi": [("NVDA", "NVIDIA"), ("TSM", "TSMC"), ("AVGO", "Broadcom"), ("AMD", "AMD"), ("VST", "Vistra"), ("CEG", "Constellation Energy")],
        "etf_div": [("SPY", "SPDR S&P 500"), ("QQQ", "Invesco QQQ"), ("SCHD", "Schwab US Dividend"), ("GLD", "SPDR Gold")],
    }

    if p_col1.button("🌟 빅테크 Top 7", width="stretch", help="AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA"):
        for s, n in PRESET_GROUPS["bigtech"]:
            api_post("/us-market/watchlist", {"symbol": s, "name": n})
        st.toast("빅테크 7개 종목이 등록되었습니다.")
        st.rerun()

    if p_col2.button("⚡ AI 반도체 & 전력 인프라", width="stretch", help="NVDA, TSM, AVGO, AMD, VST, CEG"):
        for s, n in PRESET_GROUPS["ai_semi"]:
            api_post("/us-market/watchlist", {"symbol": s, "name": n})
        st.toast("AI 반도체 및 전력 인프라 종목이 등록되었습니다.")
        st.rerun()

    if p_col3.button("💰 지수 & 배당 ETF", width="stretch", help="SPY, QQQ, SCHD, GLD"):
        for s, n in PRESET_GROUPS["etf_div"]:
            api_post("/us-market/watchlist", {"symbol": s, "name": n})
        st.toast("대표 지수 및 배당 ETF가 등록되었습니다.")
        st.rerun()

    st.divider()

    # 개별 종목 수동 등록 / 수정 폼
    with st.form("add_us_symbol_form", clear_on_submit=True):
        st.markdown("##### ➕ 개별 종목 추가 / 보유 정보 설정")
        f1, f2 = st.columns(2)
        sym_input = f1.text_input("미국 티커 (예: AAPL, NVDA, TSLA, PLTR 등)", placeholder="티커 심볼 입력").strip().upper()
        name_input = f2.text_input("종목명 (선택, 비워두면 자동 조회)", placeholder="예: Apple Inc.")

        f3, f4, f5 = st.columns(3)
        avg_price_input = f3.number_input("내 매수 평단가 ($) (미보유 시 0)", min_value=0.0, step=0.1, value=0.0, help="입력 시 손절(-6%) 및 트레일링 익절 알림이 활성화됩니다.")
        held_qty_input = f4.number_input("보유 수량 (주) (미보유 시 0)", min_value=0.0, step=1.0, value=0.0)
        weight_input = f5.number_input("목표 비중 (%) (선택)", min_value=0.0, max_value=100.0, step=1.0, value=0.0)

        notes_input = st.text_input("메모 / 투자 아이디어 (선택)", placeholder="예: 30분봉 눌림목 반등 시 분할 매수 예정")
        alert_toggle_input = st.checkbox("이 종목의 시그널 텔레그램 푸시 알림 받기", value=True)

        submitted = st.form_submit_button("💾 종목 저장 / 등록", type="primary", width="stretch")
        if submitted:
            if not sym_input:
                st.error("티커 심볼을 입력하세요.")
            else:
                payload = {
                    "symbol": sym_input,
                    "name": name_input or None,
                    "avg_entry_price": avg_price_input if avg_price_input > 0 else None,
                    "held_qty": held_qty_input if held_qty_input > 0 else 0.0,
                    "target_weight": (weight_input / 100.0) if weight_input > 0 else None,
                    "alert_enabled": 1 if alert_toggle_input else 0,
                    "notes": notes_input or "",
                }
                res = api_post("/us-market/watchlist", payload)
                if res and res.get("status") == "ok":
                    st.success(f"'{sym_input}' 종목이 모니터링 목록에 저장되었습니다.")
                    st.rerun()

    st.divider()

    # 등록된 종목 목록 테이블 및 삭제 제어
    st.markdown("##### 📋 현재 등록된 모니터링 종목 목록")
    current_list = api_get("/us-market/watchlist", timeout=10) or []
    if current_list:
        rows_to_render = []
        for it in current_list:
            rows_to_render.append({
                "티커": it["symbol"],
                "종목명": it.get("name") or it["symbol"],
                "섹터": it.get("sector") or "-",
                "내 평단가": f"${it['avg_entry_price']:,.2f}" if it.get("avg_entry_price") else "-",
                "보유 수량": f"{it.get('held_qty', 0):.1f}주" if it.get("held_qty") else "-",
                "목표 비중": f"{it['target_weight']*100:.1f}%" if it.get("target_weight") else "-",
                "알림": "🔔 ON" if it.get("alert_enabled", 1) else "🔕 OFF",
                "메모": it.get("notes") or "",
            })
        st.dataframe(pd.DataFrame(rows_to_render), use_container_width=True, hide_index=True)

        # 종목 삭제 버튼
        del_col1, del_col2 = st.columns([3, 1])
        with del_col1:
            sym_to_delete = st.selectbox("삭제할 종목 선택", [it["symbol"] for it in current_list], key="select_delete_symbol")
        with del_col2:
            st.write("")
            st.write("")
            if st.button("🗑️ 선택 종목 삭제", width="stretch"):
                res = api_delete(f"/us-market/watchlist/{sym_to_delete}")
                if res and res.get("status") == "deleted":
                    st.toast(f"'{sym_to_delete}' 종목이 삭제되었습니다.")
                    st.rerun()
    else:
        st.caption("등록된 종목이 없습니다.")


# =========================================================================
# TAB 3: 🧾 시그널 타임라인 (Signal Log)
# =========================================================================
with tab_history:
    st.subheader("🧾 30분 시그널 판정 이력 & 복기 로그")
    st.caption("매 30분 사이클마다 생성된 매수/매도/손절/익절 판단 기록을 확인합니다.")

    hist_sym_filter = st.text_input("종목 심볼로 검색 (전체는 'ALL' 입력)", value="ALL").strip().upper()
    hist_limit = st.slider("조회 건수", 20, 200, 50)

    history_records = api_get(f"/us-market/history/{hist_sym_filter or 'ALL'}?limit={hist_limit}", timeout=15) or []

    if history_records:
        df_hist = pd.DataFrame(history_records)
        df_hist["시각 (KST)"] = df_hist["ts"].apply(lambda t: fmt_kst(t, "%m-%d %H:%M:%S"))
        df_hist["종목"] = df_hist["symbol"]
        df_hist["현재가"] = df_hist["price"].apply(lambda p: f"${p:,.2f}")
        df_hist["등락률"] = df_hist["change_pct"].apply(lambda c: f"{c:+.2f}%" if c is not None else "-")

        def fmt_action(row):
            act = row.get("combined_action", "HOLD")
            strg = int((row.get("strength") or 0.0) * 100)
            ex = row.get("exit_trigger", "NONE")
            if ex == "STOP_LOSS":
                return "🚨 손절선 도달"
            elif ex == "TRAILING_TAKE_PROFIT":
                return "🎯 트레일링 익절"
            elif act == "BUY":
                return f"🟢 BUY ({strg}%)"
            elif act == "SELL":
                return f"🔴 SELL ({strg}%)"
            return f"🟡 HOLD ({strg}%)"

        df_hist["최종 시그널"] = df_hist.apply(fmt_action, axis=1)
        df_hist["판단 근거"] = df_hist["reason"]

        display_cols = ["시각 (KST)", "종목", "현재가", "등락률", "최종 시그널", "판단 근거"]
        st.dataframe(df_hist[display_cols], use_container_width=True, hide_index=True)
    else:
        st.info("아직 누적된 시그널 기록이 없습니다. 상단 '⚡ 지금 즉시 전체 스캔'을 실행해 보세요.")


# =========================================================================
# TAB 4: 🔔 알림 & 환경 설정
# =========================================================================
with tab_settings:
    st.subheader("🔔 실시간 알림 & 텔레그램 연동 설정")
    st.caption("한국 시각 야간 미국 정규장 운영 중 스마트폰 텔레그램으로 시그널을 즉시 받아볼 수 있습니다.")

    c_cfg1, c_cfg2 = st.columns(2)
    with c_cfg1:
        st.markdown("##### 📱 텔레그램 봇 연동")
        from core.config import settings
        tg_token_env = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN") or ""
        tg_chat_env = getattr(settings, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID") or ""

        tg_token_input = st.text_input("Telegram Bot Token", value=tg_token_env, type="password", help="@BotFather 에서 발급받은 봇 토큰")
        tg_chat_input = st.text_input("Telegram Chat ID", value=tg_chat_env, help="알림을 수신할 개인 또는 채널의 숫자 Chat ID (예: 123456789)")

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("💾 설정 저장 (.env)", type="primary", width="stretch"):
                with st.spinner("설정 저장 중..."):
                    save_res = api_post("/us-market/telegram/save", {"token": tg_token_input, "chat_id": tg_chat_input})
                    if save_res and save_res.get("status") == "ok":
                        st.success("✅ 텔레그램 설정이 .env에 성공적으로 저장되었습니다!")
                    else:
                        st.error("설정 저장 실패")

        with btn_col2:
            if st.button("📨 테스트 메시지 전송", type="secondary", width="stretch"):
                with st.spinner("테스트 메시지 발송 중..."):
                    test_res = api_post("/us-market/telegram/test", {"token": tg_token_input, "chat_id": tg_chat_input})
                    if test_res and test_res.get("status") == "ok":
                        st.success("✅ 텔레그램 테스트 메시지가 정상 발송되었습니다! 스마트폰을 확인하세요.")
                    else:
                        detail = test_res.get("detail", "봇 토큰과 채팅 ID를 확인하세요.") if isinstance(test_res, dict) else "발송 실패"
                        st.error(f"❌ {detail}")

    with c_cfg2:
        st.markdown("##### 🛡️ 시그널 임계값 안내 (설정값 기반)")
        st.info(
            f"- **기본 스캔 주기**: 30분 (정규장 개장 중 자동 실행)\n"
            f"- **손절(Stop-Loss) 기준**: 평단가 대비 **-6.0%** 도달 시 강제 경고\n"
            f"- **트레일링 익절 무장**: 진입 후 고점 **+3.0%** 이상 상승 시 발동 대기\n"
            f"- **트레일링 익절 폭**: 고점 대비 **-5.0%** (또는 ATR 2.5배) 하락 시 익절 권고\n"
            f"- **결합 가중치**: 30분봉(50%) + 일봉 추세(30%) + 월가 컨센서스(20%)\n"
            f"- **텔레그램 알림 기준**: 시그널 강도 **60% 이상**(적극 매수/매도) 또는 **손절/익절** 시에만 선별 발송"
        )

        st.markdown("##### 💡 텔레그램 봇 간편 생성 & 연동 필수 체크리스트")
        st.caption(
            "1. 텔레그램 검색창에서 **@BotFather** 를 검색하고 `/newbot` 입력\n"
            "2. 봇 이름과 사용자명(@..._bot) 설정 후 발급된 **HTTP API Token** 복사하여 좌측에 입력\n"
            "3. 텔레그램 검색창에서 **@userinfobot** 에 아무 메시지를 보내 내 **Id (숫자 형태의 Chat ID)** 확인 후 좌측에 입력\n"
            "4. ⚠️ **[가장 중요 - chat not found 해결법]** 내가 만든 봇(@..._bot) 대화방에 들어가서 **[시작(Start)] 버튼(또는 `/start` 전송)**을 반드시 한 번 눌러주세요!\n"
            "   *(텔레그램 보안 정책상, 사용자가 먼저 대화를 시작하지 않은 봇은 메시지를 보낼 수 없습니다)*\n"
            "5. 테스트 발송 성공 후 **[💾 설정 저장 (.env)]**을 클릭하면 다음부터 자동 저장됩니다."
        )

