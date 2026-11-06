# pages/3_미국장_모니터링.py
"""🇺🇸 미국장 시그널 룸 — 30분 주기 모니터링 & 매수/매도 시그널 알림 전용 대시보드.

해외 직투 계좌(달러 투자) 및 메리츠 해외 계좌를 위해
30분봉·일봉 기술적 지표, 월가 컨센서스, 보유자 맞춤형 손절/트레일링 익절을
실시간 추적하고 스마트 리밸런싱 및 서프라이즈 뉴스를 제공합니다.
UI 컴포넌트는 ui/us_market_ui.py를 공유합니다.
"""
from __future__ import annotations

import time
import streamlit as st

from core.config import settings
from ui.common import api_get, api_post, fmt_kst
from ui.us_market_ui import (
    inject_us_ui_styles,
    render_us_rebalance_section,
    render_us_news_surprise_section,
    render_us_signals_board_section,
    render_us_watchlist_manage_section,
    render_us_history_section,
    render_us_telegram_settings_section,
)

is_meritz = getattr(settings, "BROKER", "kis").lower() == "meritz"

st.set_page_config(layout="wide", page_title="미국장 시그널 룸", page_icon="📡")
inject_us_ui_styles()

# --- 상단 및 사이드바 제어 ---
mkt_status = api_get("/us-market/status", timeout=10)
if mkt_status is None:
    st.error(
        "제어 플레인(FastAPI)에 연결할 수 없습니다. "
        f"`uv run uvicorn api.main:app --port {settings.API_PORT}` 이 실행 중인지 확인하세요."
    )
    st.stop()

with st.sidebar:
    st.title("📡 미국장 시그널 룸")
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
    sched_toggle = st.toggle("30분 백그라운드 자동 스캔", value=sched_running, key="room_sidebar_sched_toggle")
    if sched_toggle != sched_running:
        api_post(f"/us-market/scheduler/toggle?enable={str(sched_toggle).lower()}")
        st.toast(f"백그라운드 스케줄러: {'활성화' if sched_toggle else '비활성화'}")
        st.rerun()

    auto_refresh = st.toggle("화면 자동 새로고침", value=False, key="room_sidebar_auto_refresh")
    refresh_sec = st.slider("새로고침 주기(초)", 10, 300, 30, disabled=not auto_refresh, key="room_sidebar_refresh_sec")

    if st.button("⚡ 지금 즉시 전체 스캔", width="stretch", type="primary", key="room_sidebar_scan_btn"):
        with st.spinner("미국 모니터링 종목 30분 시그널 전체 스캔 중..."):
            res = api_post("/us-market/scan", {"send_notifications": True}, timeout=60)
            if res and res.get("status") == "ok":
                st.toast(f"✅ 스캔 완료: {res.get('scanned_count')}개 종목 갱신됨")
                time.sleep(0.5)
                st.rerun()

    if st.button("🔄 화면 새로고침", width="stretch", key="room_sidebar_rerun_btn"):
        st.rerun()

# 페이지 헤더
col_h1, col_h2 = st.columns([3, 1])
with col_h1:
    st.title("📡 미국장 시그널 룸")
    st.caption("해외 직투 계좌 전용 — 30분봉 스윙 시그널 & 손절·트레일링 익절 실시간 알림")
with col_h2:
    st.write("")
    last_scan = mkt_status.get("scheduler_last_scan_at")
    scan_cnt = mkt_status.get("scheduler_last_scan_count", 0)
    if last_scan:
        st.info(f"최근 스캔: **{fmt_kst(last_scan)}** ({scan_cnt}종목)")
    else:
        st.caption("최근 자동 스캔 기록 없음")

if is_meritz:
    st.info(
        "💡 **통합 안내**: 현재 메리츠 해외 계좌(`.env.meritz`)를 사용 중입니다. "
        "모든 자동매매 주문 집행, 자산배분 및 실시간 관제는 **[메리츠 해외 자동매매]** 관제탑 페이지에서 올인원으로 제어하실 수 있습니다."
    )

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

with tab_rebalance:
    render_us_rebalance_section(key_prefix="room_reb")

with tab_news:
    render_us_news_surprise_section(key_prefix="room_news")

with tab_board:
    render_us_signals_board_section(key_prefix="room_board")

with tab_manage:
    render_us_watchlist_manage_section(key_prefix="room_manage")

with tab_history:
    render_us_history_section(key_prefix="room_hist")

with tab_settings:
    render_us_telegram_settings_section(key_prefix="room_settings")

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()
