# pages/1_KIS_자동매매.py
"""KIS 자동매매 엔진 제어 UI. FastAPI 제어 플레인(api/main.py, 기본 8800포트)을
폴링하는 thin client — 엔진 로직은 여기 없다.

먼저 `uvicorn api.main:app --port 8800` 로 제어 플레인을 띄운 뒤 이 페이지를 사용한다.
"""
import ast
import json
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

API_BASE = "http://127.0.0.1:8800"
KST = timezone(timedelta(hours=9))

st.set_page_config(layout="wide", page_title="KIS 자동매매", page_icon="🤖")

# --- 스타일 -------------------------------------------------------------
st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; max-width: 1200px; }
    div[data-testid="stMetric"] {
        background: rgba(127,127,127,0.08);
        border: 1px solid rgba(127,127,127,0.18);
        border-radius: 12px;
        padding: 14px 16px 8px 16px;
    }
    .jarvis-badge {
        display: inline-block; padding: 3px 10px; border-radius: 999px;
        font-size: 0.78rem; font-weight: 600; margin-right: 6px; color: white;
    }
    .badge-buy { background: #1f9d55; }
    .badge-sell { background: #d64545; }
    .badge-noop { background: #7a7a7a; }
    .badge-mock { background: #2f6fed; }
    .badge-live { background: #d64545; }
    .jarvis-card {
        background: rgba(127,127,127,0.06);
        border: 1px solid rgba(127,127,127,0.16);
        border-radius: 12px;
        padding: 14px 18px;
        margin-bottom: 10px;
    }
    .jarvis-card h4 { margin: 0 0 4px 0; }
    .jarvis-muted { opacity: 0.65; font-size: 0.85rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def api_get(path: str, **params):
    return requests.get(f"{API_BASE}{path}", params=params, timeout=10).json()


def api_post(path: str, json_body: dict | None = None):
    resp = requests.post(f"{API_BASE}{path}", json=json_body, timeout=10)
    if resp.status_code >= 400:
        st.error(resp.json().get("detail", resp.text))
        return None
    return resp.json()


def fmt_kst(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=KST).strftime("%m-%d %H:%M:%S")


def fmt_pct(v) -> str:
    try:
        return f"{float(v) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def parse_sentiment(raw: str | None):
    if not raw or raw == "None":
        return None
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {"raw": raw}


# --- 사이드바 -------------------------------------------------------------
with st.sidebar:
    st.title("🤖 Finance Jarvis")
    st.caption("KIS 자동매매 제어판")
    st.divider()
    auto_refresh = st.toggle("자동 새로고침", value=False)
    refresh_sec = st.slider("주기(초)", 5, 60, 15, disabled=not auto_refresh)
    if st.button("🔄 지금 새로고침", width='stretch'):
        st.rerun()

try:
    status = api_get("/engine/status")
    config = api_get("/engine/config")
except requests.exceptions.ConnectionError:
    st.error("제어 플레인(FastAPI)에 연결할 수 없습니다. `uvicorn api.main:app --port 8800`을 먼저 실행하세요.")
    st.stop()

with st.sidebar:
    mode_class = "badge-mock" if status["is_mock"] else "badge-live"
    mode_text = "모의투자" if status["is_mock"] else "⚠️ 실전투자"
    st.markdown(f'<span class="jarvis-badge {mode_class}">{mode_text}</span>', unsafe_allow_html=True)
    st.markdown(
        f'<span class="jarvis-badge {"badge-buy" if status["engine_running"] else "badge-noop"}">'
        f'{"실행 중" if status["engine_running"] else "정지"}</span>',
        unsafe_allow_html=True,
    )
    if status["kill_switch_active"]:
        st.markdown('<span class="jarvis-badge badge-sell">Kill Switch 🔴</span>', unsafe_allow_html=True)

st.title("🤖 KIS 자동매매 제어")

if not status["is_mock"]:
    st.error("⚠️ 실전투자 모드입니다 (IS_MOCK=False). 실제 자금이 거래됩니다.")

if status["kill_switch_active"]:
    st.warning(f"Kill switch 사유: {status['kill_switch_reason']}")
    if st.button("Kill switch 해제 (사람이 원인 확인 후에만)"):
        api_post("/engine/clear-kill-switch")
        st.rerun()

tab_overview, tab_settings, tab_portfolio, tab_log = st.tabs(
    ["📊 개요", "⚙️ 설정 · 리스크", "💼 포트폴리오 · 비중", "🧾 매매 로그"]
)

# =========================================================================
# 개요
# =========================================================================
with tab_overview:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("엔진 상태", "🟢 실행 중" if status["engine_running"] else "⚪ 정지")
    c2.metric("Heartbeat", f"{status['heartbeat_age_sec']:.0f}s 전" if status["heartbeat_age_sec"] else "N/A")
    c3.metric("체결통보 웹소켓", f"{status['ws_last_message_age_sec']:.0f}s 전" if status["ws_last_message_age_sec"] else "N/A")
    c4.metric("Kill Switch", "🔴 활성" if status["kill_switch_active"] else "🟢 정상")

    st.write("")
    b1, b2, b3, b4 = st.columns(4)
    if b1.button("▶️ 시작", disabled=status["engine_running"], width='stretch'):
        if api_post("/engine/start") is not None:
            st.rerun()
    if b2.button("⏸️ 정지", disabled=not status["engine_running"], width='stretch'):
        api_post("/engine/stop")
        st.rerun()
    if b3.button("🛑 긴급정지 (전량 주문취소)", disabled=not status["engine_running"], width='stretch'):
        st.session_state["confirm_kill"] = True
    if b4.button("🔄 새로고침", width='stretch'):
        st.rerun()

    if st.session_state.get("confirm_kill"):
        st.warning("정말 긴급정지하시겠습니까? 미체결 주문이 전부 취소됩니다.")
        cc1, cc2 = st.columns(2)
        if cc1.button("네, 긴급정지합니다"):
            api_post("/engine/kill")
            st.session_state["confirm_kill"] = False
            st.rerun()
        if cc2.button("취소"):
            st.session_state["confirm_kill"] = False
            st.rerun()

    st.divider()
    positions = api_get("/portfolio/positions")
    if positions:
        st.subheader("보유 종목 장부가치 비중 (참고용 — 실시간 시가 아님)")
        rows = [
            {"symbol": sym, "book_value": p["qty"] * p["avg_price"]}
            for sym, p in positions.items() if p["qty"]
        ]
        if rows:
            fig = go.Figure(data=[go.Pie(labels=[r["symbol"] for r in rows], values=[r["book_value"] for r in rows], hole=0.45)])
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=320)
            st.plotly_chart(fig, width='stretch')
        else:
            st.caption("보유 수량이 0인 종목만 있습니다.")
    else:
        st.caption("보유 포지션이 없습니다.")

# =========================================================================
# 설정 · 리스크
# =========================================================================
with tab_settings:
    st.caption("core/config.py 기본값 및 .env 오버라이드가 반영된 현재 실행 설정값입니다 (읽기 전용).")

    st.markdown("#### 🛡️ 안전장치")
    r1, r2, r3 = st.columns(3)
    r1.metric("모드", "모의투자" if config["is_mock"] else "⚠️ 실전투자")
    r2.metric("모니터링/매매 윈도우", f"{config['cycle_interval_sec'] // 60}분")
    r3.metric("종목당 재주문 쿨다운", f"{config['order_cooldown_sec'] // 60}분")

    r4, r5, r6 = st.columns(3)
    r4.metric("리밸런스 밴드", fmt_pct(config["rebalance_band_pct"]))
    r5.metric("종목당 최대 비중", fmt_pct(config["max_position_pct"]))
    r6.metric("1회 주문 최대 금액", f"{config['max_order_notional_krw']:,}원")

    r7, r8 = st.columns(2)
    r7.metric("일일 손실 한도", fmt_pct(config["max_daily_loss_pct"]))
    r8.metric("체결통보 WS 무응답 허용", f"{config['ws_staleness_threshold_sec']}초")

    st.divider()
    st.markdown("#### 🩹 손절 · 트레일링익절 (밴드/쿨다운과 무관하게 항상 우선 평가)")
    e1, e2 = st.columns(2)
    e1.metric("손절", f"평단가 대비 -{fmt_pct(config['stop_loss_pct'])}")
    e2.metric("트레일링 익절", f"고점 대비 -{fmt_pct(config['trailing_take_profit_pct'])}")

    st.divider()
    st.markdown("#### 🌀 스윙 시그널 (밴드는 보조 상한으로만 작동)")
    s1, s2, s3 = st.columns(3)
    s1.metric("스윙 진입 임계강도", config["swing_signal_threshold"])
    s2.metric("스윙 1회 최대비중", fmt_pct(config["swing_trade_max_equity_fraction"]))
    s3.metric("밴드 상한 버퍼", fmt_pct(config["band_ceiling_buffer_pct"]))
    s4, s5 = st.columns(2)
    s4.metric("분봉 캔들 단위", f"{config['intraday_bar_minutes']}분")
    s5.metric("분봉 백필 기간", f"{config['intraday_lookback_calendar_days']}일")

    st.divider()
    st.markdown("#### 🧠 LLM (뉴스 감성분석 · 비중제안) · 밸류에이션")
    l1, l2, l3 = st.columns(3)
    l1.metric("Provider", config["llm_provider"])
    l2.metric("모델", config["gemini_model"])
    l3.metric("API Key", "✅ 설정됨" if config["gemini_configured"] else "❌ 미설정 (감성분석 비활성)")

    l4, l5, l6 = st.columns(3)
    l4.metric("뉴스 lookback", f"{config['news_lookback_hours']}시간")
    l5.metric("종목당 최대 기사 수", config["news_max_articles_per_symbol"])
    l6.metric(
        "펀더멘털 밸류에이션",
        "✅ 활성" if config["enable_fundamental_valuation"] else "⏸️ 비활성 (국내 종목만 지원)",
    )

# =========================================================================
# 포트폴리오 · 비중
# =========================================================================
with tab_portfolio:
    st.markdown("#### 📋 포트폴리오 종목 등록")
    with st.form("add_symbol_form"):
        fc1, fc2, fc3 = st.columns([1, 2, 1])
        new_market = fc1.radio("구분", ["국내", "해외(미국만 실증됨)"], horizontal=False)
        new_symbol = fc2.text_input("종목코드 (예: 005930 또는 AAPL)")
        new_exchange = None
        if new_market.startswith("해외"):
            new_exchange = fc3.selectbox("거래소", ["NASD", "NYSE", "AMEX"])
        submitted = st.form_submit_button("등록")
        if submitted and new_symbol:
            payload = {"symbol": new_symbol.strip(), "market": "domestic" if new_market == "국내" else "overseas"}
            if new_exchange:
                payload["exchange"] = new_exchange
            api_post("/portfolio/symbols", payload)
            st.rerun()

    symbols = api_get("/portfolio/symbols")
    if symbols:
        st.dataframe(pd.DataFrame(symbols), width='stretch', hide_index=True)

    st.divider()
    st.markdown("#### 🎯 목표 비중")
    with st.form("set_weight_form"):
        wc1, wc2, wc3 = st.columns([2, 2, 1])
        w_symbol = wc1.text_input("종목코드")
        w_value = wc2.number_input("목표 비중 (0.0 ~ 1.0)", min_value=0.0, max_value=1.0, step=0.01)
        wc3.write("")
        wc3.write("")
        w_submitted = wc3.form_submit_button("설정 (즉시승인)")
        if w_submitted and w_symbol:
            api_post("/portfolio/weights", {"symbol": w_symbol.strip(), "weight": w_value})
            st.rerun()

    weights = api_get("/portfolio/weights")
    if weights:
        wdf = pd.DataFrame({"symbol": list(weights.keys()), "target_weight_pct": [w * 100 for w in weights.values()]})
        wc_table, wc_chart = st.columns([1, 1])
        wc_table.dataframe(
            wdf,
            width='stretch',
            hide_index=True,
            column_config={"target_weight_pct": st.column_config.NumberColumn("목표비중", format="%.2f%%")},
        )
        fig = go.Figure(data=[go.Bar(x=wdf["symbol"], y=wdf["target_weight_pct"])])
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=280, yaxis_title="%")
        wc_chart.plotly_chart(fig, width='stretch')
    else:
        st.caption("승인된 목표 비중이 없습니다.")

    st.divider()
    st.markdown("#### 🤖 LLM 비중 제안 (Gemini) — 승인 전까지 매매에 반영 안 됨")
    if st.button("LLM에게 목표비중 제안 요청"):
        result = api_post("/portfolio/weights/propose")
        if result is not None:
            st.success(result.get("rationale", "제안 완료"))
            st.rerun()

    proposals = api_get("/portfolio/weights/proposals")
    if proposals:
        for p in proposals:
            with st.container():
                st.markdown(
                    f'<div class="jarvis-card"><h4>{p["symbol"]} · {fmt_pct(p["weight"])}'
                    f' <span class="jarvis-muted">({p["proposed_by"]})</span></h4>'
                    f'<div class="jarvis-muted">{p.get("rationale", "")}</div></div>',
                    unsafe_allow_html=True,
                )
                pc1, pc2, _ = st.columns([1, 1, 4])
                if pc1.button("✅ 승인", key=f"approve_{p['id']}"):
                    api_post(f"/portfolio/weights/proposals/{p['id']}/decide", {"approve": True})
                    st.rerun()
                if pc2.button("❌ 거부", key=f"reject_{p['id']}"):
                    api_post(f"/portfolio/weights/proposals/{p['id']}/decide", {"approve": False})
                    st.rerun()
    else:
        st.caption("대기 중인 제안이 없습니다.")

    st.divider()
    st.markdown("#### 💼 현재 포지션")
    if positions:
        pdf = pd.DataFrame(
            [
                {
                    "symbol": sym,
                    "수량": p["qty"],
                    "평균단가": p["avg_price"],
                    "장부가치": p["qty"] * p["avg_price"],
                    "통화": p["currency"],
                    "동기화": fmt_kst(p["last_synced_at"]),
                }
                for sym, p in positions.items()
            ]
        )
        st.dataframe(pdf, width='stretch', hide_index=True)
    else:
        st.caption("보유 포지션이 없습니다.")

# =========================================================================
# 매매 로그
# =========================================================================
with tab_log:
    limit = st.slider("표시 개수", 10, 200, 50, step=10)
    decisions = api_get("/decisions/recent", limit=limit)

    if not decisions:
        st.caption("아직 기록된 의사결정이 없습니다.")
    else:
        all_symbols = sorted({d["symbol"] for d in decisions})
        fc1, fc2 = st.columns([2, 2])
        symbol_filter = fc1.multiselect("종목 필터", all_symbols)
        action_filter = fc2.multiselect("액션 필터", ["BUY", "SELL", "NO_OP"])

        rows = []
        for d in decisions:
            if symbol_filter and d["symbol"] not in symbol_filter:
                continue
            if action_filter and d["action"] not in action_filter:
                continue
            badge = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "NO_OP": "⚪ NO_OP"}.get(d["action"], d["action"])
            reason = d.get("reason") or "-"
            reason_badge = {
                "STOP_LOSS": "🚨 손절(STOP_LOSS)",
                "TRAILING_TAKE_PROFIT": "💰 트레일링익절",
                "swing_signal": "🌀 스윙시그널",
                "band_ceiling_forced_trim": "📉 밴드상한 강제축소",
            }.get(reason, reason)
            sentiment = parse_sentiment(d.get("sentiment_signal"))
            sentiment_txt = (
                f'{sentiment.get("direction", "?")} ({sentiment.get("strength", 0):.2f})'
                if sentiment and "direction" in sentiment
                else "-"
            )
            rows.append(
                {
                    "시각": fmt_kst(d["ts"]),
                    "종목": d["symbol"],
                    "액션": badge,
                    "수량": d.get("qty") or "-",
                    "현재비중": fmt_pct(d.get("current_weight")),
                    "목표비중": fmt_pct(d.get("target_weight")),
                    "드리프트": fmt_pct(d.get("drift")),
                    "기술시그널": d.get("tech_signal") or "-",
                    "감성시그널": sentiment_txt,
                    "사유": reason_badge,
                    "_id": d["id"],
                }
            )

        if not rows:
            st.caption("필터에 해당하는 로그가 없습니다.")
        else:
            df = pd.DataFrame(rows)
            st.dataframe(df.drop(columns=["_id"]), width='stretch', hide_index=True, height=560)

            st.divider()
            st.markdown("#### 🔍 원시 스냅샷 상세 조회")
            options = {f'{r["시각"]} · {r["종목"]} · {r["액션"]}': r["_id"] for r in rows}
            picked = st.selectbox("행 선택", list(options.keys()))
            if picked:
                target = next(d for d in decisions if d["id"] == options[picked])
                try:
                    ctx = json.loads(target.get("context_json") or "{}")
                except json.JSONDecodeError:
                    ctx = {"raw": target.get("context_json")}
                st.json(ctx)

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()
