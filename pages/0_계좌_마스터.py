# pages/0_계좌_마스터.py
"""Finance Jarvis 멀티 계좌 마스터 관제 허브 (Master Hub).

등록된 모든 계좌 프로필(ISA, 실전 일반, 모의투자)의 실시간 프로세스 상태,
백엔드 엔진 및 대시보드 구동 여부, 실시간 자산/손익을 종합 모니터링하고
원클릭으로 On/Off, 재기동, 기본 프로필 전환을 제어합니다.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List

import streamlit as st

from core.config import settings
from core.profile_manager import (
    clear_kill_switch,
    get_active_profile,
    get_all_profiles_info,
    get_session_output,
    get_tmux_sessions,
    is_master_session_running,
    restart_profile,
    start_component,
    start_master_portal,
    start_profile,
    stop_component,
    stop_profile,
    switch_profile,
    trigger_engine_loop,
)

st.set_page_config(layout="wide", page_title="계좌 마스터 허브", page_icon="🏢")

# --- 커스텀 스타일 ---
st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; max-width: 1300px; }
    .hub-metric-card {
        background: rgba(127,127,127,0.06);
        border: 1px solid rgba(127,127,127,0.18);
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 12px;
    }
    .profile-card {
        background: rgba(127,127,127,0.05);
        border: 1px solid rgba(127,127,127,0.18);
        border-radius: 14px;
        padding: 20px 22px;
        margin-bottom: 20px;
        transition: transform 0.15s ease, border-color 0.15s ease;
    }
    .profile-card:hover {
        border-color: rgba(66, 133, 244, 0.45);
    }
    .badge-live {
        background: #dc2626; color: white; padding: 3px 8px; border-radius: 6px;
        font-size: 0.76rem; font-weight: bold; margin-right: 6px;
    }
    .badge-mock {
        background: #2563eb; color: white; padding: 3px 8px; border-radius: 6px;
        font-size: 0.76rem; font-weight: bold; margin-right: 6px;
    }
    .badge-active {
        background: #10b981; color: white; padding: 3px 8px; border-radius: 6px;
        font-size: 0.76rem; font-weight: bold;
    }
    .status-pill {
        display: inline-block; padding: 4px 10px; border-radius: 999px;
        font-size: 0.82rem; font-weight: 600;
    }
    .pill-running { background: rgba(16, 185, 129, 0.18); color: #10b981; border: 1px solid #10b981; }
    .pill-partial { background: rgba(245, 158, 11, 0.18); color: #f59e0b; border: 1px solid #f59e0b; }
    .pill-stopped { background: rgba(107, 114, 128, 0.18); color: #9ca3af; border: 1px solid #6b7280; }
    .log-terminal {
        background-color: #111827;
        color: #f3f4f6;
        font-family: 'Consolas', 'Courier New', monospace;
        padding: 14px;
        border-radius: 8px;
        font-size: 0.80rem;
        line-height: 1.45;
        max-height: 420px;
        overflow-y: auto;
        white-space: pre-wrap;
        word-break: break-all;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# --- 헤더 영역 ---
header_col1, header_col2 = st.columns([3, 1])
with header_col1:
    st.title("🏢 계좌 마스터 관제 허브")
    st.caption("멀티 계좌 프로필(ISA / 실전 일반 / 모의투자) 실시간 구동 제어 및 종합 관제탑")

with header_col2:
    st.write("")
    if st.button("🔄 전체 상태 새로고침", use_container_width=True, type="secondary"):
        st.rerun()

# --- 프로필 데이터 조회 ---
profiles = get_all_profiles_info()
active_prof = get_active_profile()
current_front_port = getattr(settings, "FRONT_PORT", 8501)

# 통계 집계
total_holdings = sum(p["holdings_value"] for p in profiles)
total_unrealized = sum(p["unrealized_pnl"] for p in profiles)
running_profiles_count = sum(1 for p in profiles if p["status_code"] == "running")
total_profiles_count = len(profiles)

# --- 상단 통합 KPI 요약 카드 ---
kpi_col1, kpi_col2, kpi_col3, kpi_col4 = st.columns(4)

with kpi_col1:
    st.metric(
        label="💼 총 운용자산 합계",
        value=f"{total_holdings:,.0f} 원",
        help="가동 중인 모든 계좌의 주식/ETF 평가금액 합계",
    )

with kpi_col2:
    pnl_prefix = "+" if total_unrealized > 0 else ""
    st.metric(
        label="📊 총 평가손익 합계",
        value=f"{pnl_prefix}{total_unrealized:,.0f} 원",
        delta=f"{pnl_prefix}{total_unrealized:,.0f} 원" if total_unrealized != 0 else None,
        help="가동 중인 모든 계좌의 실시간 합산 미실현 손익",
    )

with kpi_col3:
    st.metric(
        label="🟢 가동 중 프로필",
        value=f"{running_profiles_count} / {total_profiles_count} 계좌",
        help="엔진과 대시보드가 모두 정상 가동 중인 프로필 수",
    )

with kpi_col4:
    active_display = next((p["display_name"] for p in profiles if p["is_active"]), active_prof)
    st.metric(
        label="⭐ 기본 활성 프로필",
        value=active_display,
        delta=f"ID: {active_prof}",
        help="현재 CLI 및 기본 .env에 바인딩된 프로필",
    )

st.markdown("---")

# --- 일괄 제어 및 빠른 액션 툴바 ---
toolbar_col1, toolbar_col2, toolbar_col3 = st.columns([2, 1, 1])

with toolbar_col1:
    st.subheader("📋 계좌별 실시간 관제 및 제어")

with toolbar_col2:
    if st.button("🚀 미실행 계좌 일괄 기동", use_container_width=True, help="정지되어 있는 모든 계좌를 즉시 기동합니다."):
        with st.spinner("미실행 프로필을 기동하는 중..."):
            started_names = []
            for p in profiles:
                if p["status_code"] != "running":
                    start_profile(p["name"])
                    started_names.append(p["display_name"])
            if started_names:
                st.success(f"기동 완료: {', '.join(started_names)}")
            else:
                st.info("이미 모든 계좌가 가동 중입니다.")
            time.sleep(1)
            st.rerun()

with toolbar_col3:
    is_master_up = is_master_session_running()
    if is_master_up:
        st.markdown(
            f'<a href="http://localhost:8500" target="_blank" style="text-decoration:none;">'
            f'<button style="width:100%; height:38px; border-radius:8px; background:#4f46e5; color:white; border:none; font-weight:600; cursor:pointer;">'
            f'🌐 마스터 포털(:8500) 열기</button></a>',
            unsafe_allow_html=True,
        )
    else:
        if st.button("🏢 마스터 포털(:8500) 기동", use_container_width=True, help="모든 계좌를 꺼도 유지되는 24/7 전용 관제 포털을 기동합니다."):
            ok, msg = start_master_portal()
            if ok:
                st.success(msg)
                time.sleep(1)
                st.rerun()
            else:
                st.error(msg)

st.write("")

# --- 계좌별 상세 제어 카드 목록 ---
for p in profiles:
    name = p["name"]
    display = p["display_name"]
    is_mock = p["is_mock"]
    acc_no = p["account_no"]
    api_port = p["api_port"]
    front_port = p["front_port"]
    status_code = p["status_code"]
    is_active = p["is_active"]
    is_current_view = (front_port == current_front_port)

    # 테두리 및 뱃지 스타일
    card_border = "#10b981" if status_code == "running" else ("#f59e0b" if status_code != "stopped" else "rgba(127,127,127,0.25)")
    type_badge_html = '<span class="badge-mock">모의</span>' if is_mock else '<span class="badge-live">실전</span>'
    active_badge_html = '<span class="badge-active">⭐ 기본 프로필</span>' if is_active else ''
    current_badge_html = '<span style="background:#6366f1; color:white; padding:3px 8px; border-radius:6px; font-size:0.76rem; font-weight:bold; margin-left:6px;">📍 현재 접속 중</span>' if is_current_view else ''

    status_pill_class = "pill-running" if status_code == "running" else ("pill-partial" if status_code != "stopped" else "pill-stopped")

    with st.container():
        st.markdown(
            f"""
            <div class="profile-card" style="border-left: 6px solid {card_border};">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div>
                        {type_badge_html}
                        <strong style="font-size: 1.18rem; margin-right: 8px;">{display}</strong>
                        <code style="font-size: 0.88rem; color: #888;">({name})</code>
                        {active_badge_html}
                        {current_badge_html}
                    </div>
                    <div>
                        <span class="status-pill {status_pill_class}">{p["status_desc"]}</span>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col_info, col_actions = st.columns([3, 2])

        with col_info:
            subcol1, subcol2, subcol3 = st.columns(3)
            with subcol1:
                st.caption("계좌번호 / HTS")
                st.markdown(f"**`{acc_no}`**")
                st.caption(f"통신 포트: API `:{api_port}` | UI `:{front_port}`")

            with subcol2:
                st.caption("프로세스 및 루프 상태")
                engine_icon = "🟢" if p["engine_running"] else "⚪"
                front_icon = "🟢" if p["front_running"] else "⚪"
                loop_icon = "🟢" if p["engine_loop_running"] else "⏸️"
                ws_icon = "🟢" if p["ws_alive"] else "⚪"
                st.markdown(f"엔진: {engine_icon} | 대시보드: {front_icon}")
                st.markdown(f"매매루프: {loop_icon} | 웹소켓: {ws_icon}")

            with subcol3:
                st.caption("실시간 자산 및 손익")
                if p["api_online"]:
                    h_val = p["holdings_value"]
                    u_pnl = p["unrealized_pnl"]
                    pnl_color = "#10b981" if u_pnl > 0 else ("#ef4444" if u_pnl < 0 else "#888")
                    st.markdown(f"**{h_val:,.0f} 원**")
                    st.markdown(f"<span style='color:{pnl_color}; font-weight:600;'>{u_pnl:+,.0f} 원</span>", unsafe_allow_html=True)
                else:
                    st.markdown("<span style='color:#888;'>API 정지됨</span>", unsafe_allow_html=True)

        with col_actions:
            st.caption("원클릭 프로필 제어")
            btn_c1, btn_c2, btn_c3, btn_c4 = st.columns(4)

            # 기동 / 중지 버튼
            with btn_c1:
                if status_code != "running":
                    if st.button("🟢 기동", key=f"start_{name}", use_container_width=True, type="primary"):
                        with st.spinner(f"[{display}] 기동 중..."):
                            ok, msg = start_profile(name)
                            if ok:
                                st.toast(f"{display} 기동 완료!", icon="🚀")
                            else:
                                st.error(msg)
                            time.sleep(1)
                            st.rerun()
                else:
                    if st.button("🔴 중지", key=f"stop_{name}", use_container_width=True):
                        if is_current_view:
                            st.warning("⚠️ 현재 접속 중인 대시보드입니다. 중지 시 본 화면과의 연결이 종료됩니다.")
                        with st.spinner(f"[{display}] 중지 중..."):
                            ok, msg = stop_profile(name)
                            if ok:
                                st.toast(f"{display} 세션이 중지되었습니다.", icon="🛑")
                            else:
                                st.warning(msg)
                            time.sleep(1)
                            st.rerun()

            # 재기동 버튼
            with btn_c2:
                if st.button("🔄 재기동", key=f"restart_{name}", use_container_width=True, help="엔진 및 대시보드를 재부팅합니다."):
                    with st.spinner(f"[{display}] 재기동 중..."):
                        ok, msg = restart_profile(name)
                        if ok:
                            st.toast(f"{display} 재기동 완료!", icon="🔄")
                        else:
                            st.error(msg)
                        time.sleep(1.2)
                        st.rerun()

            # 기본 프로필 전환
            with btn_c3:
                if not is_active:
                    if st.button("⭐ 지정", key=f"switch_{name}", use_container_width=True, help="기본 활성 프로필(.env)로 전환"):
                        ok, msg = switch_profile(name)
                        if ok:
                            st.toast(f"기본 프로필을 [{name}]으로 변경했습니다.", icon="⭐")
                            time.sleep(0.5)
                            st.rerun()
                        else:
                            st.error(msg)
                else:
                    st.button("⭐ 기본", key=f"active_lbl_{name}", use_container_width=True, disabled=True)

            # 대시보드 열기 링크
            with btn_c4:
                dashboard_url = f"http://localhost:{front_port}"
                if p["front_running"]:
                    st.markdown(
                        f'<a href="{dashboard_url}" target="_blank" style="text-decoration:none;">'
                        f'<button style="width:100%; height:38px; border-radius:8px; background:#2563eb; color:white; border:none; font-weight:600; cursor:pointer;">'
                        f'🔗 이동</button></a>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.button("🔗 꺼짐", key=f"dash_off_{name}", use_container_width=True, disabled=True)

        # 고급 세부 제어 Expander
        with st.expander(f"⚙️ [{display}] 세부 제어 및 진단 (컴포넌트 분리 관리)"):
            adv_c1, adv_c2, adv_c3, adv_c4 = st.columns(4)

            with adv_c1:
                st.markdown("**엔진 프로세스 (FastAPI)**")
                if not p["engine_running"]:
                    if st.button("⚡ 엔진만 켜기", key=f"adv_start_eng_{name}", use_container_width=True):
                        start_component(name, "engine")
                        time.sleep(0.8)
                        st.rerun()
                else:
                    if st.button("🛑 엔진만 끄기", key=f"adv_stop_eng_{name}", use_container_width=True):
                        stop_component(name, "engine")
                        time.sleep(0.8)
                        st.rerun()

            with adv_c2:
                st.markdown("**대시보드 (Streamlit)**")
                if not p["front_running"]:
                    if st.button("📊 대시보드만 켜기", key=f"adv_start_front_{name}", use_container_width=True):
                        start_component(name, "front")
                        time.sleep(0.8)
                        st.rerun()
                else:
                    if st.button("🛑 대시보드만 끄기", key=f"adv_stop_front_{name}", use_container_width=True):
                        stop_component(name, "front")
                        time.sleep(0.8)
                        st.rerun()

            with adv_c3:
                st.markdown("**트레이딩 루프 (엔진)**")
                if p["api_online"]:
                    if not p["engine_loop_running"]:
                        if st.button("▶️ 루프 시작", key=f"adv_loop_start_{name}", use_container_width=True):
                            ok, msg = trigger_engine_loop(api_port, "start")
                            if ok:
                                st.success(msg)
                            else:
                                st.error(msg)
                            time.sleep(0.5)
                            st.rerun()
                    else:
                        if st.button("⏸️ 루프 일시정지", key=f"adv_loop_stop_{name}", use_container_width=True):
                            ok, msg = trigger_engine_loop(api_port, "stop")
                            if ok:
                                st.info(msg)
                            else:
                                st.error(msg)
                            time.sleep(0.5)
                            st.rerun()
                else:
                    st.caption("엔진 프로세스가 꺼져 있습니다.")

            with adv_c4:
                st.markdown("**안전 가드 / 킬스위치**")
                if p["kill_switch"]:
                    st.error("🚨 킬스위치 활성화 상태")
                    if st.button("🔓 킬스위치 해제", key=f"adv_clear_kill_{name}", use_container_width=True):
                        ok, msg = clear_kill_switch(api_port)
                        if ok:
                            st.success(msg)
                        else:
                            st.error(msg)
                        time.sleep(0.5)
                        st.rerun()
                else:
                    st.caption("킬스위치 정상 (미발동)")

        st.markdown("<div style='margin-bottom: 16px;'></div>", unsafe_allow_html=True)

st.markdown("---")

# --- 실시간 터미널 콘솔 로그 뷰어 ---
st.subheader("🖥️ 실시간 세션 콘솔 로그 뷰어")
st.caption("터미널에 접속하지 않고도 각 백그라운드 세션(엔진 및 대시보드)의 표준 출력을 실시간으로 확인합니다.")

tmux_sessions = sorted(list(get_tmux_sessions()))

if tmux_sessions:
    log_col1, log_col2, log_col3 = st.columns([2, 1, 1])
    with log_col1:
        selected_session = st.selectbox("관제할 tmux 세션 선택", tmux_sessions, index=0)
    with log_col2:
        log_lines = st.selectbox("출력 줄 수", [30, 50, 100, 200], index=1)
    with log_col3:
        st.write("")
        refresh_logs = st.button("🔄 로그 새로고침", use_container_width=True)

    session_log_content = get_session_output(selected_session, lines=log_lines)
    st.markdown(
        f'<div class="log-terminal">{session_log_content}</div>',
        unsafe_allow_html=True,
    )
else:
    st.info("현재 실행 중인 tmux 세션이 없습니다.")

