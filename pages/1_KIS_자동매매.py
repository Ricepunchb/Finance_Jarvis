# pages/1_KIS_자동매매.py
"""KIS 자동매매 엔진 제어 UI. FastAPI 제어 플레인(api/main.py, 기본 8800포트)을
폴링하는 thin client — 엔진 로직은 여기 없다.

먼저 `uvicorn api.main:app --port 8800` 로 제어 플레인을 띄운 뒤 이 페이지를 사용한다.
"""
import streamlit as st
import requests

API_BASE = "http://127.0.0.1:8800"

st.set_page_config(layout="wide", page_title="KIS 자동매매")


def api_get(path: str, **params):
    return requests.get(f"{API_BASE}{path}", params=params, timeout=10).json()


def api_post(path: str, json: dict | None = None):
    resp = requests.post(f"{API_BASE}{path}", json=json, timeout=10)
    if resp.status_code >= 400:
        st.error(resp.json().get("detail", resp.text))
        return None
    return resp.json()


st.title("🤖 KIS 자동매매 제어")

try:
    status = api_get("/engine/status")
except requests.exceptions.ConnectionError:
    st.error("제어 플레인(FastAPI)에 연결할 수 없습니다. `uvicorn api.main:app --port 8800`을 먼저 실행하세요.")
    st.stop()

if not status["is_mock"]:
    st.error("⚠️ 실전투자 모드입니다 (IS_MOCK=False). 실제 자금이 거래됩니다.")
else:
    st.info("모의투자 모드입니다.")

col1, col2, col3, col4 = st.columns(4)
col1.metric("엔진 상태", "🟢 실행 중" if status["engine_running"] else "⚪ 정지")
col2.metric("Heartbeat", f"{status['heartbeat_age_sec']:.0f}s 전" if status["heartbeat_age_sec"] else "N/A")
col3.metric("체결통보 웹소켓", f"{status['ws_last_message_age_sec']:.0f}s 전" if status["ws_last_message_age_sec"] else "N/A")
col4.metric("Kill Switch", "🔴 활성" if status["kill_switch_active"] else "🟢 정상")

if status["kill_switch_active"]:
    st.warning(f"Kill switch 사유: {status['kill_switch_reason']}")
    if st.button("Kill switch 해제 (사람이 원인 확인 후에만)"):
        api_post("/engine/clear-kill-switch")
        st.rerun()

st.divider()
b1, b2, b3, b4 = st.columns(4)
if b1.button("▶️ 시작", disabled=status["engine_running"]):
    if api_post("/engine/start") is not None:
        st.rerun()
if b2.button("⏸️ 정지", disabled=not status["engine_running"]):
    api_post("/engine/stop")
    st.rerun()
if b3.button("🛑 긴급정지 (전량 주문취소)", disabled=not status["engine_running"]):
    st.session_state["confirm_kill"] = True
if b4.button("🔄 새로고침"):
    st.rerun()

if st.session_state.get("confirm_kill"):
    st.warning("정말 긴급정지하시겠습니까? 미체결 주문이 전부 취소됩니다.")
    c1, c2 = st.columns(2)
    if c1.button("네, 긴급정지합니다"):
        api_post("/engine/kill")
        st.session_state["confirm_kill"] = False
        st.rerun()
    if c2.button("취소"):
        st.session_state["confirm_kill"] = False
        st.rerun()

st.divider()
st.header("📋 포트폴리오 종목 등록")
with st.form("add_symbol_form"):
    new_market = st.radio("구분", ["국내", "해외(미국만 실증됨)"], horizontal=True)
    new_symbol = st.text_input("종목코드 (예: 005930 또는 AAPL)")
    new_exchange = None
    if new_market.startswith("해외"):
        new_exchange = st.selectbox("거래소", ["NASD", "NYSE", "AMEX"])
    submitted = st.form_submit_button("등록")
    if submitted and new_symbol:
        payload = {"symbol": new_symbol.strip(), "market": "domestic" if new_market == "국내" else "overseas"}
        if new_exchange:
            payload["exchange"] = new_exchange
        api_post("/portfolio/symbols", payload)
        st.rerun()

symbols = api_get("/portfolio/symbols")
if symbols:
    st.dataframe(symbols, use_container_width=True)

st.divider()
st.header("🎯 목표 비중 설정 (수동 입력 즉시 승인 — Phase 1)")
with st.form("set_weight_form"):
    w_symbol = st.text_input("종목코드")
    w_value = st.number_input("목표 비중 (0.0 ~ 1.0)", min_value=0.0, max_value=1.0, step=0.01)
    w_submitted = st.form_submit_button("설정")
    if w_submitted and w_symbol:
        api_post("/portfolio/weights", {"symbol": w_symbol.strip(), "weight": w_value})
        st.rerun()

weights = api_get("/portfolio/weights")
if weights:
    st.table(weights)

st.divider()
st.header("🤖 LLM 비중 제안 (Gemini) — 승인 전까지 매매에 반영 안 됨")
if st.button("LLM에게 목표비중 제안 요청"):
    result = api_post("/portfolio/weights/propose")
    if result is not None:
        st.success(result.get("rationale", "제안 완료"))
        st.rerun()

proposals = api_get("/portfolio/weights/proposals")
if proposals:
    for p in proposals:
        c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
        c1.write(f"**{p['symbol']}**")
        c2.write(f"제안비중: {p['weight']:.2%}")
        if c3.button("승인", key=f"approve_{p['id']}"):
            api_post(f"/portfolio/weights/proposals/{p['id']}/decide", {"approve": True})
            st.rerun()
        if c4.button("거부", key=f"reject_{p['id']}"):
            api_post(f"/portfolio/weights/proposals/{p['id']}/decide", {"approve": False})
            st.rerun()
        st.caption(p.get("rationale", ""))
else:
    st.write("대기 중인 제안이 없습니다.")

st.divider()
st.header("💼 현재 포지션")
positions = api_get("/portfolio/positions")
if positions:
    st.dataframe(positions, use_container_width=True)
else:
    st.write("보유 포지션이 없습니다.")

st.divider()
st.header("🧾 최근 의사결정 로그")
decisions = api_get("/decisions/recent", limit=50)
if decisions:
    st.dataframe(decisions, use_container_width=True)
else:
    st.write("아직 기록된 의사결정이 없습니다.")
