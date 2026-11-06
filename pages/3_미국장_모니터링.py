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
tab_rebalance, tab_news, tab_board, tab_manage, tab_history, tab_settings = st.tabs(
    [
        "⚖️ 자산배분 & 자동 리밸런싱",
        "⚡ 글로벌 뉴스 서프라이즈",
        "📊 30분 실시간 시그널 보드",
        "💼 관심 & 보유 종목 관리",
        "🧾 시그널 타임라인",
        "🔔 알림 & 환경 설정",
    ]
)


# =========================================================================
# TAB 1: ⚖️ 자산배분 & 자동 리밸런싱 (미국장 전용)
# =========================================================================
with tab_rebalance:
    st.subheader("⚖️ 미국장 스마트 자산배분 & 자동 리밸런싱")
    st.caption(
        "순수 USD 기준 계좌로 운영되며, **달러 입금 시 세금 없는 매수 우선(Cash-Inflow)**, "
        "**비중 초과(±5%p) 시 슬림화(Band Drift)**, **정규장 정수 1주 단위 분할 집행**을 수행합니다."
    )

    reb_status = api_get("/us-market/rebalance/status", timeout=15)
    if not reb_status:
        st.warning("미국장 포트폴리오 비중 상태를 불러올 수 없습니다. FastAPI 서버를 확인하세요.")
    else:
        tot_eq = float(reb_status.get("total_equity_usd") or 0.0)
        tot_stock = float(reb_status.get("total_stock_eval_usd") or 0.0)
        avail_usd = float(reb_status.get("available_usd") or 0.0)
        inflow_usd = float(reb_status.get("inflow_detected_usd") or 0.0)
        pos_list = reb_status.get("positions", [])

        # 1. 상단 핵심 자산 지표 (달러 기준)
        rm1, rm2, rm3, rm4 = st.columns(4)
        rm1.metric("총 평가자산 (USD)", f"${tot_eq:,.2f}")
        rm2.metric("주식 평가액 (USD)", f"${tot_stock:,.2f}")
        rm3.metric(
            "가용 달러 (Available USD)",
            f"${avail_usd:,.2f}",
            delta=f"+${inflow_usd:,.2f} 입금 감지" if inflow_usd > 10.0 else None,
        )
        excess_cnt = len(reb_status.get("excess_positions", []))
        deficit_cnt = len(reb_status.get("deficit_positions", []))
        rm4.metric(
            "포트폴리오 균형 상태",
            "균형 유지 (정상)" if excess_cnt == 0 and deficit_cnt == 0 else f"초과 {excess_cnt} / 미달 {deficit_cnt}",
            delta="리밸런싱 권장" if excess_cnt > 0 or (avail_usd >= 30.0 and deficit_cnt > 0) else "안정",
            delta_color="normal" if excess_cnt == 0 else "inverse",
        )

        st.divider()

        # 2. 포트폴리오 비중 현황 테이블
        st.markdown("##### 📊 종목별 현재 비중 vs 목표 비중 현황")
        rows = []
        for p in pos_list:
            sym = p.get("symbol", "")
            name = p.get("name", sym)
            qty = p.get("qty", 0.0)
            prc = p.get("current_price", 0.0)
            val = p.get("eval_value_usd", 0.0)
            cur_w = p.get("current_weight", 0.0) * 100
            tgt_w = p.get("target_weight", 0.0) * 100
            drift = p.get("drift_pct", 0.0) * 100
            stat = p.get("status", "BALANCED")

            if stat == "EXCESS":
                stat_badge = "🔴 과비중 (+5%p 초과)"
            elif stat == "EXCESS_LOCKED":
                stat_badge = "🟢 유지 (1주 단가 보호)"
            elif stat == "DEFICIT":
                stat_badge = "🔵 저비중 (-5%p 미달)"
            else:
                stat_badge = "🟢 균형 (정상)"

            rows.append({
                "티커": sym,
                "종목명": name,
                "보유 수량": f"{qty:.0f}주",
                "현재가": f"${prc:,.2f}",
                "평가액": f"${val:,.2f}",
                "현재 비중": f"{cur_w:.1f}%",
                "목표 비중": f"{tgt_w:.1f}%",
                "괴리(Drift)": f"{drift:+.1f}%p",
                "상태": stat_badge,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.divider()

        # 3. 리밸런싱 실행 및 텔레그램 연동 제어
        st.markdown("##### ⚡ 스마트 리밸런싱 진단 & 실행 제어")
        col_act1, col_act2, col_act3 = st.columns([1.5, 1.5, 2])

        with col_act1:
            btn_plan = st.button("🔍 리밸런싱 계획 수립/새로고침", use_container_width=True, type="secondary")
        with col_act2:
            btn_notify = st.button("📨 텔레그램 브리핑 발송", use_container_width=True)
        with col_act3:
            btn_execute = st.button("🚀 리밸런싱 즉시 실행 (정규장 주문)", use_container_width=True, type="primary")

        # 계획 수립
        plan_res = api_post("/us-market/rebalance/plan", {"force_inflow_rebalance": False})

        if btn_notify:
            with st.spinner("텔레그램으로 리밸런싱 계획 발송 중..."):
                notify_res = api_post("/us-market/rebalance/notify", {})
                if notify_res and notify_res.get("status") == "ok":
                    st.success("✅ 텔레그램으로 리밸런싱 계획 브리핑이 성공적으로 전송되었습니다!")
                else:
                    detail = notify_res.get("detail", "전송 실패") if isinstance(notify_res, dict) else "전송 실패"
                    st.error(f"❌ 텔레그램 발송 실패: {detail}")

        if btn_execute:
            with st.spinner("리밸런싱 매매 주문 집행 중 (매도 우선 -> 매수 순서)..."):
                exec_res = api_post("/us-market/rebalance/execute", {})
                if exec_res and exec_res.get("status") in ("SUCCESS", "SKIPPED"):
                    st.success(f"리밸런싱 집행 완료: {exec_res.get('executed_count', 0)}건 체결 완료!")
                    time.sleep(1)
                    st.rerun()
                else:
                    detail = exec_res.get("message", "주문 오류") if isinstance(exec_res, dict) else "집행 실패"
                    st.error(f"집행 실패: {detail}")

        # 계획 표시
        if plan_res and plan_res.get("items"):
            trigger_t = plan_res.get("trigger_type", "")
            if trigger_t == "CASH_INFLOW":
                banner_cls = "recommendation-box"
                tag_str = "💡 달러 신규 입금 감지 (기존 주식 매도 0건 — 양도소득세 없는 신규 달러 매수 우선)"
            elif trigger_t == "BAND_DRIFT":
                banner_cls = "recommendation-box-sell"
                tag_str = "⚠️ 비중 허용오차(±5%p) 이탈 감지 (과비중 초과분 분할 매도 및 저비중 재배치)"
            else:
                banner_cls = "recommendation-box"
                tag_str = "📋 목표 비중 채움 리밸런싱"

            st.markdown(
                f'<div class="{banner_cls}" style="margin-bottom:15px;">'
                f'<b>{tag_str}</b><br>'
                f'<span style="font-size:0.88rem; color:#d1d5db;">{plan_res.get("rationale", "")}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            p_col1, p_col2 = st.columns(2)
            with p_col1:
                st.markdown("###### 🔴 매도 예정 (과비중 슬림화)")
                sells = [it for it in plan_res["items"] if it["action"] == "SELL"]
                if sells:
                    for s in sells:
                        st.write(
                            f"- **{s['symbol']}**: **{s['qty']}주** (약 ${s['estimated_amount_usd']:,.2f}) "
                            f"— {s['reason']}"
                        )
                else:
                    st.caption("매도 예정 내역 없음 (세금 발생 없음)")

            with p_col2:
                st.markdown("###### 🟢 매수 예정 (저비중 채움)")
                buys = [it for it in plan_res["items"] if it["action"] == "BUY"]
                if buys:
                    for b in buys:
                        st.write(
                            f"- **{b['symbol']}**: **{b['qty']}주** (약 ${b['estimated_amount_usd']:,.2f}) "
                            f"— {b['reason']}"
                        )
                else:
                    st.caption("매수 예정 내역 없음")

            st.caption(
                f"총 매도 회수액: **${plan_res.get('total_sell_usd', 0):,.2f}** | "
                f"총 매수 투입액: **${plan_res.get('total_buy_usd', 0):,.2f}** | "
                f"예상 잔여 달러: **${plan_res.get('projected_residual_usd', 0):,.2f}**"
            )
        else:
            st.info("현재 모든 종목이 목표 비중 허용 오차(±5%p) 이내이며, 추가 리밸런싱이 필요하지 않습니다.")

        # 4. 목표 비중 편집기
        with st.expander("⚙️ 미국장 포트폴리오 목표 비중(Target Weights) 편집", expanded=False):
            st.caption("각 종목의 목표 비중을 백분율(%)로 입력하세요. 총합은 자동으로 100%로 정규화됩니다.")
            cur_targets = {p["symbol"]: round(p["target_weight"] * 100, 1) for p in pos_list}
            w_inputs = {}

            c_w1, c_w2 = st.columns(2)
            for idx, (sym, def_w) in enumerate(cur_targets.items()):
                target_col = c_w1 if idx % 2 == 0 else c_w2
                with target_col:
                    w_inputs[sym] = st.number_input(
                        f"{sym} 목표 비중 (%)",
                        min_value=0.0,
                        max_value=100.0,
                        value=float(def_w),
                        step=1.0,
                        key=f"target_w_{sym}",
                    )

            if st.button("💾 목표 비중 저장 및 적용", type="primary"):
                total_w = sum(w_inputs.values())
                if total_w <= 0:
                    st.error("최소 1개 이상의 종목 비중이 0보다 커야 합니다.")
                else:
                    norm_weights = {k: v / total_w for k, v in w_inputs.items()}
                    res = api_post("/us-market/rebalance/weights", {"weights": norm_weights})
                    if res and res.get("status") == "ok":
                        st.success("✅ 미국장 목표 비중이 성공적으로 저장되었습니다!")
                        time.sleep(1)
                        st.rerun()


# =========================================================================
# TAB 2: ⚡ 글로벌 뉴스 서프라이즈 인텔리전스
# =========================================================================
with tab_news:
    st.subheader("⚡ 글로벌 뉴스 서프라이즈 인텔리전스")
    st.caption(
        "로이터(Reuters), 블룸버그(Bloomberg) 등 주요 외신 뉴스를 실시간 수집 및 사건 단위로 자동 군집화(Clustering)하고, "
        "시장 기대치 괴리도와 주가 반응을 결합한 **Surprise 점수(0~100점)**를 산출하여 핵심 요약을 전달합니다."
    )

    ctrl_col1, ctrl_col2, ctrl_col3 = st.columns([1.5, 2, 1.5])
    with ctrl_col1:
        news_sym_filter = st.text_input("종목 심볼 필터 (전체는 'ALL')", value="ALL", key="news_sym_filter").strip().upper()
    with ctrl_col2:
        threshold_slider = st.slider(
            "Surprise 점수 텔레그램 발송 기준선 (Threshold)",
            min_value=50,
            max_value=95,
            value=75,
            step=5,
            help="권장: 75점 (75점 이상 유의미한 시장 충격/호재·악재 뉴스만 텔레그램으로 즉시 발송됩니다)",
            key="news_threshold_slider",
        )
    with ctrl_col3:
        st.write("")
        st.write("")
        if st.button("⚡ 뉴스 즉시 스캔 & 분석", type="primary", use_container_width=True, key="btn_scan_news"):
            with st.spinner("글로벌 뉴스 수집, 사건 군집화 및 AI 서프라이즈 점수 산출 중..."):
                scan_res = api_post(
                    "/us-market/scan-news",
                    {
                        "symbols": None if news_sym_filter in ("", "ALL") else [news_sym_filter],
                        "threshold": threshold_slider,
                        "send_notifications": True,
                    },
                    timeout=30,
                )
                if scan_res and scan_res.get("status") == "ok":
                    alerted = scan_res.get("alerted_count", 0)
                    total_art = scan_res.get("total_articles", 0)
                    clusters_n = scan_res.get("clusters_count", 0)
                    st.success(
                        f"스캔 완료: 총 {total_art}개 기사 수집 ➔ {clusters_n}개 사건 군집화 ➔ "
                        f"기준선({threshold_slider}점) 이상 {alerted}건 서프라이즈 알림 발송!"
                    )
                    time.sleep(1)
                    st.rerun()
                else:
                    detail = scan_res.get("detail", "서버 응답 오류") if isinstance(scan_res, dict) else "스캔 실패"
                    st.error(f"뉴스 스캔 실패: {detail}")

    st.divider()

    # 뉴스 알림 목록 조회
    filter_arg = None if news_sym_filter in ("", "ALL") else news_sym_filter
    url = f"/us-market/news-alerts?limit=50"
    if filter_arg:
        url += f"&symbol={filter_arg}"

    news_alerts = api_get(url, timeout=10) or []

    if news_alerts:
        st.markdown(f"##### 📢 포착된 서프라이즈 뉴스 피드 (최근 {len(news_alerts)}건)")
        for n_item in news_alerts:
            n_sym = n_item.get("symbol", "")
            n_title = n_item.get("title", "")
            n_score = int(n_item.get("surprise_score", 0))
            n_impact = n_item.get("impact_type", "중립")
            n_sector = n_item.get("affected_sector", "글로벌 거시경제")
            n_source = n_item.get("source", "외신 종합")
            n_clusters = int(n_item.get("cluster_count", 1))
            n_sent_at = n_item.get("sent_at")
            n_summary = n_item.get("summary", "")

            # 영향 배지 스타일
            if n_impact == "호재":
                impact_badge = '<span class="badge badge-buy-strong" style="font-size:0.85rem;">호재</span>'
                card_border = "#10b981"
            elif n_impact == "악재":
                impact_badge = '<span class="badge badge-sell-strong" style="font-size:0.85rem;">악재</span>'
                card_border = "#ef4444"
            else:
                impact_badge = '<span class="badge badge-hold" style="font-size:0.85rem;">중립</span>'
                card_border = "#6b7280"

            # 서프라이즈 점수 색상
            if n_score >= 85:
                score_badge = f'<span style="background:#dc2626; color:white; padding:3px 10px; border-radius:999px; font-weight:700; font-size:0.85rem;">Surprise {n_score}점 (초대형 임팩트)</span>'
            elif n_score >= 75:
                score_badge = f'<span style="background:#f59e0b; color:white; padding:3px 10px; border-radius:999px; font-weight:700; font-size:0.85rem;">Surprise {n_score}점 (유의미한 서프라이즈)</span>'
            else:
                score_badge = f'<span style="background:#6b7280; color:white; padding:3px 10px; border-radius:999px; font-weight:700; font-size:0.85rem;">Surprise {n_score}점</span>'

            # 클러스터 매체 레이블
            src_display = n_source
            if n_clusters > 1 and "개 매체" not in src_display:
                src_display += f" (외 {n_clusters - 1}개 매체 보도)"

            # 요약 문장 분리
            summary_lines = [line.strip() for line in n_summary.split("\n") if line.strip()]
            summary_html_items = "".join([f"<li style='margin-bottom:4px;'>{line}</li>" for line in summary_lines[:3]])

            news_card_html = (
                f'<div style="background:rgba(127,127,127,0.05); border:1px solid rgba(127,127,127,0.18); '
                f'border-left:5px solid {card_border}; border-radius:12px; padding:16px 20px; margin-bottom:14px;">'
                f'<div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; margin-bottom:8px;">'
                f'<div>'
                f'<span style="font-size:1.25rem; font-weight:800; margin-right:10px;">[{n_sym}]</span>'
                f'{impact_badge} {score_badge}'
                f'</div>'
                f'<div style="text-align:right; font-size:0.82rem; color:gray;">'
                f'보도/발송: {fmt_kst(n_sent_at)}'
                f'</div>'
                f'</div>'
                f'<div style="font-size:1.05rem; font-weight:700; margin-bottom:6px;">{n_title}</div>'
                f'<div style="font-size:0.84rem; color:#9ca3af; margin-bottom:10px;">'
                f'영향 섹터/테마: <b style="color:#d1d5db;">{n_sector}</b> &nbsp;|&nbsp; 출처: {src_display}'
                f'</div>'
                f'<div style="background:rgba(0,0,0,0.15); border-radius:8px; padding:10px 14px; font-size:0.92rem; line-height:1.5;">'
                f'<ol style="margin:0; padding-left:18px;">{summary_html_items}</ol>'
                f'</div>'
                f'</div>'
            )
            st.html(news_card_html)
    else:
        st.info(
            f"아직 기준선({threshold_slider}점) 이상의 서프라이즈 뉴스가 포착되지 않았습니다.\n\n"
            f"우측 상단의 **'⚡ 뉴스 즉시 스캔 & 분석'** 버튼을 눌러 글로벌 뉴스를 즉시 수집하고 분석해 보세요!"
        )


# =========================================================================
# TAB 2: 📊 30분 실시간 시그널 보드
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

