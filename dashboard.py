"""매매 복기·분석 대시보드 (읽기 전용) — app.py의 st.navigation이 이 파일을 "매매 복기·분석" 페이지로 연결한다.

자동매매 조작은 pages/1_KIS_자동매매.py, 이 화면은 쌓인 체결·판단·리밸런싱 기록을 돌아보는 용도다.
모든 데이터는 FastAPI(/analytics/*)에서 받는다: uvicorn api.main:app --port 8800
"""
import streamlit as st

from ui import calendar_tab, overview, rebalance, replay
from ui.common import API_BASE, api_get, cached_get, clear_cache, fmt_kst

st.set_page_config(layout="wide", page_title="Finance Jarvis 분석", page_icon="📈")
st.markdown(
    """
    <style>
    .block-container { max-width: 1300px; }
    div[data-testid="stMetric"] { border: 1px solid rgba(128,128,128,0.25); border-radius: 10px; padding: 10px 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.title("📈 Finance Jarvis")
    st.caption("매매 복기·분석 (읽기 전용)")
    days = st.selectbox("분석 기간", [7, 14, 30, 60, 90], index=2, format_func=lambda d: f"최근 {d}일")
    st.toggle("상승=빨강 · 하락=파랑 (한국식)", value=True, key="korean_colors",
              help="끄면 상승=파랑 · 하락=빨강. 숫자에는 항상 +/− 부호가 함께 표시됩니다.")
    force = st.button("🔄 데이터 새로고침", width="stretch", help="KIS 일봉을 다시 조회합니다. 모의계좌는 1초당 1회 제한이라 오래 걸릴 수 있습니다.")
    if force:
        clear_cache()
    st.divider()
    st.caption(f"API: {API_BASE}")

if api_get("/engine/status", timeout=5) is None:
    st.error("제어 플레인(FastAPI)에 연결할 수 없습니다. `uv run uvicorn api.main:app --port 8800` 으로 먼저 실행하세요.")
    st.stop()

names = cached_get("/portfolio/symbol-names", ttl=3600, timeout=60) or {}
with st.spinner("성과를 계산하는 중입니다… (처음에는 KIS 일봉 조회로 오래 걸릴 수 있습니다)"):
    data = cached_get("/analytics/overview", ttl=120, timeout=600, fresh=force, days=days)
if data is None:
    st.error("분석 데이터를 불러오지 못했습니다. API 로그를 확인하세요.")
    st.stop()

st.title("📈 매매 복기·분석")
st.caption(f"계산 시각 {fmt_kst(data['generated_at'], '%Y-%m-%d %H:%M:%S')} · 읽기 전용 — 주문·설정 변경은 'KIS 자동매매' 페이지에서")

symbols = [r["symbol"] for r in data["symbols"]]
tab_overview, tab_calendar, tab_replay, tab_rebalance = st.tabs(["📊 성과 오버뷰", "📅 매매 달력", "🧠 의사결정 복기", "🔁 리밸런싱 이력"])
with tab_overview:
    overview.render(data, names)
with tab_calendar:
    calendar_tab.render(data["range"].get("data_start"), symbols, names)
with tab_replay:
    replay.render(symbols, data["range"].get("data_start"), names)
with tab_rebalance:
    rebalance.render(names)
