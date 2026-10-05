# pages/2_인사이트.py
"""💡 AI 인사이트 — 증권사 리포트·시장 뉴스·발굴 결과를 묶음(batch) 단위로 통합 분석하고 조회하는 대시보드."""
import streamlit as st

from ui import insights_tab
from ui.common import API_BASE, api_get, api_post, cached_get, clear_cache, fmt_kst, symbol_label

st.set_page_config(layout="wide", page_title="Finance Jarvis AI 인사이트", page_icon="💡")
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
    st.title("💡 AI 인사이트")
    st.caption("증권사 리포트 · 뉴스 · 발굴 묶음 분석")
    force = st.button("🔄 데이터 새로고침", width="stretch")
    if force:
        clear_cache()
    st.divider()

if api_get("/engine/status", timeout=5) is None:
    st.error("제어 플레인(FastAPI)에 연결할 수 없습니다. `uv run uvicorn api.main:app --port 8800` 으로 먼저 실행하세요.")
    st.stop()

names = cached_get("/portfolio/symbol-names", ttl=3600, timeout=60) or {}
batches = cached_get("/insights/batches", ttl=60, timeout=30, fresh=force) or []

if not batches:
    st.title("💡 AI 인사이트")
    st.info(
        "아직 생성된 인사이트 묶음이 없습니다.\n\n"
        "장 마감 후 16시 정기 스케줄러가 돌거나, 'KIS 자동매매' 탭에서 **[🔎 지금 발굴 갱신]**을 누르면 "
        "증권사 리포트 본문 수집, 뉴스 헤드라인, 보유종목 감성 분석, 발굴 결과가 하나의 묶음으로 생성됩니다."
    )
    st.stop()

# 사이드바 묶음 선택
batch_options = {}
for b in batches:
    time_str = fmt_kst(b["started_at"], "%m-%d %H:%M")
    trig_str = "정기" if b["trigger_type"] == "SCHEDULED" else "수동"
    stat_str = "✅" if b["status"] == "DONE" else ("⚠️" if b["status"] == "PARTIAL" else "⏳")
    new_rep_cnt = b.get("counts", {}).get("new_report", 0)
    label = f"#{b['id']} · {time_str} ({trig_str}) {stat_str} · 신규 리포트 {new_rep_cnt}건"
    batch_options[b["id"]] = label

with st.sidebar:
    st.markdown("##### 📅 인사이트 묶음 선택")
    selected_batch_id = st.selectbox(
        "분석 회차",
        options=list(batch_options.keys()),
        format_func=lambda bid: batch_options[bid],
        index=0,
    )
    st.divider()
    st.caption("종목 타임라인 빠른 검색")
    search_sym = st.text_input("종목코드 (예: 005930)", value="").strip()
    st.divider()
    st.caption(f"API: {API_BASE}")

# 선택된 묶음 상세 로드
detail = cached_get(f"/insights/batches/{selected_batch_id}", ttl=60, timeout=60, fresh=force)
if not detail:
    st.error(f"묶음 #{selected_batch_id} 데이터를 불러오지 못했습니다.")
    st.stop()

counts = detail.get("counts") or {}
items = detail.get("items") or {}
digest = detail.get("digest")

st.title(f"💡 AI 인사이트 #{detail['id']}")
meta_str = f"수집 시각 {fmt_kst(detail['started_at'], '%Y-%m-%d %H:%M')} · 상태: {detail['status']} ({detail['trigger_type']})"
st.caption(meta_str)

# KPI 카드 5개
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("신규 리포트", f"{counts.get('new_report', 0)}건", help="이 묶음에서 처음 확인된 증권사 리포트")
k2.metric("전체 리포트", f"{counts.get('broker_report', 0)}건", help="최근 14일 lookback 내 수집된 전체 리포트")
k3.metric("시장 헤드라인", f"{counts.get('market_headline', 0)}건", help="KIS 시황·공시 표본 중 종목 매칭 기사")
k4.metric("보유종목 뉴스", f"{counts.get('holding_news', 0)}건", help="보유 종목에 대한 신규 감성 분석 뉴스")
k5.metric(
    "발굴 숏리스트",
    f"{counts.get('shortlist', 0)} / {counts.get('candidate', 0)}",
    help="복합점수 상위 숏리스트 / 전체 평가 후보",
)

# AI 다이제스트 카드
render_col1, render_col2 = st.columns([5, 1])
with render_col2:
    if st.button("🔄 다이제스트 재생성", help="Gemini를 호출하여 종합 AI 다이제스트를 다시 작성합니다."):
        with st.spinner("AI 다이제스트 작성 중..."):
            res = api_post(f"/insights/batches/{detail['id']}/digest")
            if res and res.get("status") == "ok":
                st.success("다이제스트가 업데이트되었습니다.")
                clear_cache()
                st.rerun()
            else:
                st.error("다이제스트 생성 실패")

insights_tab.render_digest_card(digest, names)

# 탭 메뉴 구성
tab_report, tab_headline, tab_news, tab_candidate, tab_timeline = st.tabs(
    [
        f"📑 증권사 리포트 ({counts.get('broker_report', 0)})",
        f"📰 시장 헤드라인 ({counts.get('market_headline', 0)})",
        f"🗞️ 보유종목 뉴스 ({counts.get('holding_news', 0)})",
        f"🎯 발굴 결과 ({counts.get('candidate', 0)})",
        "🔎 종목 타임라인",
    ]
)

with tab_report:
    insights_tab.render_broker_reports(items.get("broker_report", []), names)

with tab_headline:
    insights_tab.render_market_headlines(items.get("market_headline", []), names)

with tab_news:
    insights_tab.render_holding_news(items.get("holding_news", []), names)

with tab_candidate:
    insights_tab.render_candidates(items.get("candidate", []), names)

with tab_timeline:
    if search_sym:
        target_sym = search_sym
    else:
        sym_candidates = sorted({
            i.get("symbol") for i_list in items.values() for i in i_list if i.get("symbol")
        })
        target_sym = st.selectbox(
            "타임라인 조회할 종목 선택",
            options=sym_candidates,
            format_func=lambda s: symbol_label(s, names),
        ) if sym_candidates else ""

    if target_sym:
        insights_tab.render_symbol_timeline(target_sym, names)
