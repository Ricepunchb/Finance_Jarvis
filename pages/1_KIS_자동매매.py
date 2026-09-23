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


def api_get(path: str, timeout: int = 20, **params):
    """실패(타임아웃/연결끊김/4xx·5xx)는 예외를 올리지 않고 None을 반환한다.
    호출부는 기존 `if x:` 패턴으로 자연스럽게 폴백 처리하도록 통일."""
    try:
        resp = requests.get(f"{API_BASE}{path}", params=params, timeout=timeout)
    except requests.exceptions.RequestException:
        return None
    if resp.status_code >= 400:
        return None
    return resp.json()


def api_post(path: str, json_body: dict | None = None):
    # /engine/start·/engine/kill은 reconciliation·웹소켓 연결·주문취소처럼 수 초~수십 초
    # 걸리는 작업을 응답 전에 끝마치므로 기본 10초보다 넉넉한 타임아웃이 필요하다.
    timeout = 60 if path in ("/engine/start", "/engine/kill") else 10
    try:
        resp = requests.post(f"{API_BASE}{path}", json=json_body, timeout=timeout)
    except requests.exceptions.Timeout:
        st.warning(
            f"{timeout}초 안에 응답이 없습니다 — 서버 쪽에서는 계속 처리 중일 수 있으니 "
            "새로고침으로 실제 상태(engine_running)를 먼저 확인하세요."
        )
        return None
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

status = api_get("/engine/status")
config = api_get("/engine/config")
if status is None or config is None:
    st.error(
        "제어 플레인(FastAPI)에 연결할 수 없습니다 (타임아웃 포함). "
        "`uvicorn api.main:app --port 8800`을 먼저 실행하세요."
    )
    st.stop()

# 국내 종목이 많으면 KIS 조회가 순차적으로 이뤄져 기본 타임아웃보다 오래 걸릴 수 있어 넉넉히 잡는다.
symbol_names = api_get("/portfolio/symbol-names", timeout=45) or {}


def label(symbol: str) -> str:
    """국내종목은 'AAPL' 대신 '삼성전자(005930)'처럼 한글명을 붙여 보여준다.
    조회 실패/해외종목은 코드 그대로 폴백."""
    name = symbol_names.get(symbol)
    return f"{name}({symbol})" if name else symbol

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

tab_overview, tab_settings, tab_portfolio, tab_ai, tab_log = st.tabs(
    ["📊 개요", "⚙️ 설정 · 리스크", "💼 포트폴리오 · 비중", "🤖 AI 에이전트", "🧾 매매 로그"]
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
            fig = go.Figure(data=[go.Pie(labels=[label(r["symbol"]) for r in rows], values=[r["book_value"] for r in rows], hole=0.45)])
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
    st.caption(f"백업 모델(5xx 발생시): {config['gemini_fallback_model'] or '미설정 (폴백 없음)'}")

    l4, l5, l6 = st.columns(3)
    l4.metric("뉴스 lookback", f"{config['news_lookback_hours']}시간")
    l5.metric("종목당 최대 기사 수", config["news_max_articles_per_symbol"])
    l6.metric(
        "펀더멘털 밸류에이션",
        "✅ 활성" if config["enable_fundamental_valuation"] else "⏸️ 비활성 (국내 종목만 지원)",
    )

    st.divider()
    st.markdown("#### 🤖 AI 포트폴리오 에이전트 (제안 검증 한도 · 트리거)")
    a1, a2, a3 = st.columns(3)
    a1.metric("1회 제안 최대 회전율", fmt_pct(config["ai_rebalance_max_turnover_pct"]))
    a2.metric("종목당 최대 비중변화", fmt_pct(config["ai_rebalance_max_weight_delta_pct"]))
    a3.metric("포트폴리오 최대 종목수", config["ai_rebalance_max_portfolio_symbols"])
    a4, a5, a6 = st.columns(3)
    a4.metric("1회 최대 신규편입", config["ai_rebalance_max_symbols_added"])
    a5.metric("1회 최대 제외", config["ai_rebalance_max_symbols_removed"])
    a6.metric("종목당 최소 비중", fmt_pct(config["ai_rebalance_min_symbol_weight_pct"]))
    st.caption(f"발굴 스크리닝 상위 노출 종목수: {config['discovery_top_n']}개")

    a7, a8 = st.columns(2)
    a7.metric("에이전트 자체 결정 쿨다운", f"{config['ai_rebalance_min_interval_sec'] // 3600}시간")
    a8.metric("정기 재검토 주기", f"{config['ai_rebalance_periodic_interval_days']}일")
    a9, a10 = st.columns(2)
    a9.metric("드리프트 트리거 버퍼", fmt_pct(config["ai_rebalance_drift_trigger_buffer_pct"]))
    a10.metric("뉴스 트리거 강도 임계치", config["ai_rebalance_news_trigger_strength"])
    st.caption(f"무위험수익률(연, 샤프/소티노 계산용): {fmt_pct(config['risk_free_rate_annual'])}")

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
        symbols_df = pd.DataFrame(symbols)
        symbols_df.insert(1, "종목명", symbols_df["symbol"].map(lambda s: symbol_names.get(s, "-")))
        st.dataframe(symbols_df, width='stretch', hide_index=True)

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
        wdf = pd.DataFrame({
            "symbol": list(weights.keys()),
            "종목명": [symbol_names.get(sym, "-") for sym in weights.keys()],
            "target_weight_pct": [w * 100 for w in weights.values()],
        })
        wc_table, wc_chart = st.columns([1, 1])
        wc_table.dataframe(
            wdf,
            width='stretch',
            hide_index=True,
            column_config={"target_weight_pct": st.column_config.NumberColumn("목표비중", format="%.2f%%")},
        )
        fig = go.Figure(data=[go.Bar(x=[label(sym) for sym in weights.keys()], y=wdf["target_weight_pct"])])
        fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=280, yaxis_title="%")
        wc_chart.plotly_chart(fig, width='stretch')
    else:
        st.caption("승인된 목표 비중이 없습니다.")

    st.divider()
    st.markdown("#### 💼 현재 포지션")
    if positions:
        pdf = pd.DataFrame(
            [
                {
                    "symbol": sym,
                    "종목명": symbol_names.get(sym, "-"),
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
# AI 에이전트
# =========================================================================
with tab_ai:
    st.markdown("#### 🧭 리밸런싱 제안 — 승인 전까지 매매에 반영 안 됨")
    if st.button("리밸런싱 제안 요청 (재비중 + 종목 발굴)"):
        result = api_post("/portfolio/weights/propose")
        if result is not None:
            msg = result.get("rationale", "제안 완료")
            if result.get("adds"):
                msg += f"\n\n신규 편입 제안: {', '.join(label(s) for s in result['adds'])}"
            if result.get("removes"):
                msg += f"\n제외 제안: {', '.join(label(s) for s in result['removes'])}"
            st.success(msg)
            st.rerun()

    # target_weights는 종목 단위지만, 종목 추가/제외가 걸린 제안은 rebalance_event 단위로
    # 원자적으로 승인해야 portfolio_symbols/논거까지 함께 반영된다 - 이벤트별로 묶어서 보여준다.
    proposals = api_get("/portfolio/weights/proposals") or []
    events_by_id = {e["id"]: e for e in (api_get("/portfolio/rebalance-events", limit=50) or [])}
    grouped: dict = {}
    for p in proposals:
        grouped.setdefault(p.get("rebalance_event_id"), []).append(p)

    if grouped:
        for event_id, rows in grouped.items():
            event = events_by_id.get(event_id) if event_id is not None else None
            adds = json.loads(event["symbols_added"]) if event and event.get("symbols_added") else []
            removes = json.loads(event["symbols_removed"]) if event and event.get("symbols_removed") else []
            with st.container():
                weight_lines = "".join(
                    f"<div>{label(r['symbol'])} → {fmt_pct(r['weight'])}"
                    + (" <span class='jarvis-muted'>(제외)</span>" if r["weight"] == 0 else "")
                    + "</div>"
                    for r in sorted(rows, key=lambda r: -r["weight"])
                )
                extra = ""
                if adds:
                    extra += f"<div class='jarvis-muted'>➕ 신규 편입: {', '.join(label(a['symbol']) for a in adds)}</div>"
                if removes:
                    extra += f"<div class='jarvis-muted'>➖ 제외: {', '.join(label(r['symbol']) for r in removes)}</div>"
                rationale = rows[0].get("rationale", "")
                st.markdown(
                    f'<div class="jarvis-card"><h4>제안 #{event_id if event_id is not None else "-"} '
                    f'<span class="jarvis-muted">({rows[0]["proposed_by"]})</span></h4>'
                    f'{weight_lines}{extra}'
                    f'<div class="jarvis-muted" style="margin-top:6px">{rationale}</div></div>',
                    unsafe_allow_html=True,
                )
                pc1, pc2, _ = st.columns([1, 1, 4])
                if event_id is not None:
                    if pc1.button("✅ 전체 승인", key=f"approve_event_{event_id}"):
                        api_post(f"/portfolio/rebalance-events/{event_id}/decide", {"approve": True})
                        st.rerun()
                    if pc2.button("❌ 전체 거부", key=f"reject_event_{event_id}"):
                        api_post(f"/portfolio/rebalance-events/{event_id}/decide", {"approve": False})
                        st.rerun()
                else:
                    # rebalance_event_id가 없는 옛 형식 제안 (마이그레이션 이전 잔여분) - 종목 단위로 폴백
                    for r in rows:
                        if st.button(f"✅ {label(r['symbol'])} 승인", key=f"approve_{r['id']}"):
                            api_post(f"/portfolio/weights/proposals/{r['id']}/decide", {"approve": True})
                            st.rerun()
    else:
        st.caption("대기 중인 제안이 없습니다.")

    with st.expander("🕓 리밸런싱 이벤트 이력 (감사로그 - 승인/거부와 무관하게 전부 기록됨)"):
        events = api_get("/portfolio/rebalance-events", limit=50) or []
        if events:
            edf = pd.DataFrame(
                [
                    {
                        "시각": fmt_kst(e["created_at"]),
                        "트리거": e["trigger_type"],
                        "상태": e["status"],
                        "사유/오류": e.get("error") or "-",
                        "결정시각": fmt_kst(e.get("decided_at")),
                        "결정자": e.get("decided_by") or "-",
                    }
                    for e in events
                ]
            )
            st.dataframe(edf, width='stretch', hide_index=True, height=280)
        else:
            st.caption("이력이 없습니다.")

    st.divider()
    st.markdown("#### ⏱️ 자동 트리거 스케줄러")
    st.caption(
        "꺼져 있어도(기본값) 위의 수동 제안 버튼은 그대로 동작한다 — 이 토글은 정기/드리프트/"
        "뉴스이벤트를 감지해 '자동으로' 제안을 생성할지만 결정하며, 최종 승인은 항상 사람 몫이다."
    )
    scheduler_status = api_get("/ai-rebalance/scheduler")
    if scheduler_status is not None:
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("스케줄러", "🟢 켜짐" if scheduler_status["enabled"] else "⚪ 꺼짐")
        sc2.metric("마지막 자동 결정", fmt_kst(scheduler_status["last_decision_at"]))
        sc3.metric("마지막 정기실행", fmt_kst(scheduler_status["last_scheduled_run_at"]))
        st.caption(
            f"정기 재검토 주기: {scheduler_status['periodic_interval_days']}일 · "
            f"에이전트 자체 결정 쿨다운: {scheduler_status['min_interval_sec'] // 3600}시간"
        )
        sb1, sb2 = st.columns(2)
        if sb1.button("🟢 켜기", disabled=scheduler_status["enabled"], width='stretch'):
            api_post("/ai-rebalance/scheduler", {"enabled": True})
            st.rerun()
        if sb2.button("⚪ 끄기", disabled=not scheduler_status["enabled"], width='stretch'):
            api_post("/ai-rebalance/scheduler", {"enabled": False})
            st.rerun()

    st.divider()
    st.markdown("#### 🔭 종목 발굴 후보 (candidate_universe)")
    st.caption("LLM은 이 목록 안에서만 신규 편입을 제안할 수 있다 — 목록 밖 종목코드는 환각으로 간주해 차단된다.")
    dc1, dc2 = st.columns([1, 3])
    if dc1.button("시드 파일 적재 (data/candidate_universe_seed.json)"):
        seed_result = api_post("/discovery/seed")
        if seed_result is not None:
            st.success(f"추가 {seed_result['added']} · 반려 {seed_result['rejected']} · 이미 있음 {seed_result['skipped']}")
            st.rerun()

    with st.form("add_candidate_form"):
        cf1, cf2, cf3 = st.columns([2, 2, 1])
        cand_symbol = cf1.text_input("종목코드 (국내만 지원)")
        cand_name = cf2.text_input("종목명 (선택 - KIS 실제명과 대조 검증)")
        cf3.write("")
        cf3.write("")
        cand_submitted = cf3.form_submit_button("후보 추가")
        if cand_submitted and cand_symbol:
            add_result = api_post("/discovery/candidates", {"symbol": cand_symbol.strip(), "name": cand_name.strip() or None})
            if add_result is not None:
                st.success(f"{add_result['name']}({add_result['symbol']}) 후보 추가됨")
                st.rerun()

    candidates = api_get("/discovery/candidates") or []
    if candidates:
        cdf = pd.DataFrame(
            [
                {
                    "종목명": c.get("name") or "-",
                    "symbol": c["symbol"],
                    "태그": c["universe_tag"],
                    "검증시각": fmt_kst(c.get("validated_at")),
                    "추가시각": fmt_kst(c.get("added_at")),
                }
                for c in candidates
            ]
        )
        st.dataframe(cdf, width='stretch', hide_index=True)
    else:
        st.caption("발굴 후보가 없습니다 - 위 시드 적재 버튼으로 초기 유니버스를 채워보세요.")

    st.divider()
    st.markdown("#### 📈 보유종목 성과 · 리스크 지표")
    st.caption(
        "AI 리밸런싱 제안이 LLM에 넘기는 것과 같은 지표(종목당 일봉 재조회 필요 - KIS 호출량 때문에 "
        "버튼을 눌러야 조회됨). 매매 판단(손절/스윙시그널)에는 관여하지 않는 참고용 지표다."
    )
    if st.button("성과지표 조회/새로고침"):
        st.session_state["performance_data"] = api_get("/portfolio/performance", timeout=60)

    perf = st.session_state.get("performance_data")
    if perf:
        perf_df = pd.DataFrame(
            [
                {
                    "종목": label(p["symbol"]),
                    "비중": fmt_pct(p.get("weight")),
                    "평단가": p.get("avg_price"),
                    "보유일": p.get("days_held", "-"),
                    "ROI": fmt_pct((p["roi_pct"] / 100) if "roi_pct" in p else None),
                    "CAGR": fmt_pct((p["cagr_pct"] / 100) if "cagr_pct" in p else None),
                    "기간수익률(90일)": fmt_pct((p["period_return_pct"] / 100) if "period_return_pct" in p else None),
                    "MDD": fmt_pct((p["mdd_pct"] / 100) if "mdd_pct" in p else None),
                    "변동성(연)": fmt_pct((p["volatility_pct"] / 100) if "volatility_pct" in p else None),
                    "샤프": round(p["sharpe"], 2) if "sharpe" in p else "-",
                    "소티노": round(p["sortino"], 2) if "sortino" in p else "-",
                    "베타(KODEX200)": round(p["beta"], 2) if "beta" in p else "-",
                    "RSI": round(p["rsi"], 1) if "rsi" in p else "-",
                }
                for p in perf
            ]
        )
        st.dataframe(perf_df, width='stretch', hide_index=True)
    elif perf is not None:
        st.caption("보유종목(승인된 목표비중 대상)이 없습니다.")

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
        symbol_filter = fc1.multiselect("종목 필터", all_symbols, format_func=label)
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
                    "종목": label(d["symbol"]),
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
