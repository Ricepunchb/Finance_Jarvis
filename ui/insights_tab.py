# ui/insights_tab.py
"""💡 AI 인사이트 탭 렌더링 모듈 — 증권사 리포트, 시장 헤드라인, 보유종목 뉴스, 발굴 결과 및 종목 타임라인."""
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from ui import theme
from ui.common import KST, cached_get, clear_cache, fmt_krw, fmt_kst, fmt_pct, symbol_label

STANCE_BADGE = {
    "POSITIVE": "🟢 긍정 (매수)",
    "NEUTRAL": "🟡 중립 (관망)",
    "NEGATIVE": "🔴 부정 (주의)",
    "CAUTION": "🟠 경계",
}


def render_digest_card(digest: Optional[Dict[str, Any]], names: Dict[str, str]) -> None:
    if not digest:
        st.info("이 묶음의 AI 종합 다이제스트가 아직 생성되지 않았습니다.")
        return

    st.markdown(
        f"""
        <div style="background: rgba(30, 41, 59, 0.5); border: 1px solid rgba(148, 163, 184, 0.2);
                    border-radius: 12px; padding: 18px 22px; margin-bottom: 18px;">
            <div style="font-size: 1.15rem; font-weight: 700; color: #38bdf8; margin-bottom: 8px;">
                💡 {digest.get('headline', '')}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        themes = digest.get("themes") or []
        if themes:
            st.markdown("##### 🏷️ 포착된 핵심 테마")
            for t in themes:
                with st.expander(f"**{t.get('theme')}** ({len(t.get('symbols', []))}개 종목 연관)"):
                    st.caption(f"근거: {t.get('evidence', '')}")
                    if t.get("symbols"):
                        st.markdown(f"**관련 종목:** {', '.join(t.get('symbols'))}")

        notables = digest.get("notable_symbols") or []
        if notables:
            st.markdown("##### 🌟 주목할 종목")
            for n in notables:
                sym_str = symbol_label(n.get("symbol", ""), names)
                st_badge = STANCE_BADGE.get(n.get("stance", ""), n.get("stance", ""))
                st.markdown(f"- **{sym_str}** `{st_badge}`: {n.get('why', '')}")

    with c2:
        risks = digest.get("risks") or []
        if risks:
            st.markdown("##### ⚠️ 시장 및 업종 주요 리스크")
            for r in risks:
                st.markdown(f"- {r}")

        watch = digest.get("holdings_watch") or []
        if watch:
            st.markdown("##### 🎯 현재 보유 종목 체크포인트")
            for w in watch:
                st.markdown(f"- {w}")


def render_broker_reports(reports: List[Dict[str, Any]], names: Dict[str, str]) -> None:
    if not reports:
        st.info("수집된 증권사 리포트가 없습니다.")
        return

    col_filter1, col_filter2, col_filter3 = st.columns([1, 1, 1])
    with col_filter1:
        only_new = st.checkbox("이번 갱신 신규 리포트만 보기", value=True)
    with col_filter2:
        brokers = sorted({r.get("source") for r in reports if r.get("source")})
        selected_broker = st.selectbox("증권사 필터", ["전체"] + brokers)
    with col_filter3:
        stances = ["전체", "POSITIVE", "NEUTRAL", "NEGATIVE"]
        selected_stance = st.selectbox("LLM 판정 필터", stances)

    filtered = reports
    if only_new:
        filtered = [r for r in filtered if r.get("is_new")]
    if selected_broker != "전체":
        filtered = [r for r in filtered if r.get("source") == selected_broker]
    if selected_stance != "전체":
        filtered = [
            r for r in filtered
            if (r.get("analysis") or r.get("payload", {}).get("analysis", {})).get("stance") == selected_stance
        ]

    st.caption(f"총 {len(filtered)}건 표시 (전체 수집 {len(reports)}건)")

    for idx, r in enumerate(filtered):
        sym = r.get("symbol", "")
        sym_name = symbol_label(sym, names)
        broker = r.get("source") or r.get("payload", {}).get("broker", "")
        title = r.get("title", "")
        write_date = r.get("payload", {}).get("write_date", "")
        analysis = r.get("analysis") or r.get("payload", {}).get("analysis", {})
        stance = analysis.get("stance") if analysis else None
        stance_badge = STANCE_BADGE.get(stance, "분석중" if not analysis else stance)

        target_price = r.get("target_price") or r.get("payload", {}).get("target_price")
        price_at_write = r.get("price_at_write") or r.get("payload", {}).get("price_at_write")
        gap_str = ""
        if target_price and price_at_write and price_at_write > 0:
            gap = (target_price - price_at_write) / price_at_write
            gap_str = f" (상승여력 {gap:+.1%})"

        is_new_badge = "🆕 " if r.get("is_new") else ""
        header = f"{is_new_badge}[{broker}] {sym_name} — '{title}' | {stance_badge}"

        with st.expander(header):
            meta_parts = []
            if write_date:
                meta_parts.append(f"작성일: {write_date}")
            if r.get("opinion") or r.get("payload", {}).get("opinion"):
                meta_parts.append(f"투자의견: {r.get('opinion') or r.get('payload', {}).get('opinion')}")
            if target_price:
                meta_parts.append(f"목표주가: {target_price:,.0f}원{gap_str}")
            if price_at_write:
                meta_parts.append(f"작성일주가: {price_at_write:,.0f}원")
            read_count = r.get("payload", {}).get("read_count")
            if read_count:
                meta_parts.append(f"조회수: {read_count:,}")
            st.caption(" · ".join(meta_parts))

            if analysis:
                st.markdown(f"**AI 요약:** {analysis.get('summary', '')}")
                kp = analysis.get("key_points") or []
                if kp:
                    st.markdown("**핵심 분석 포인트:**")
                    for p in kp:
                        st.markdown(f"- {p}")
                cat = analysis.get("catalysts") or []
                if cat:
                    st.markdown("**상승/개선 촉매:**")
                    for c in cat:
                        st.markdown(f"- 📈 {c}")
                risks = analysis.get("risks") or []
                if risks:
                    st.markdown("**리스크 / 우려 사항:**")
                    for rk in risks:
                        st.markdown(f"- ⚠️ {rk}")

            content_text = r.get("content_text")
            if content_text:
                with st.expander("리포트 본문 원문 발췌"):
                    st.text(content_text)

            links = []
            end_url = r.get("end_url") or r.get("url") or r.get("payload", {}).get("end_url")
            if end_url:
                links.append(f"[🌐 네이버 증권 리포트 페이지]({end_url})")
            attach_url = r.get("attach_url") or r.get("payload", {}).get("attach_url")
            if attach_url:
                links.append(f"[📄 원문 PDF 다운로드]({attach_url})")
            if links:
                st.markdown(" · ".join(links))


def render_market_headlines(headlines: List[Dict[str, Any]], names: Dict[str, str]) -> None:
    if not headlines:
        st.info("수집된 시장 뉴스 헤드라인 표본이 없습니다.")
        return

    st.caption("KIS 실시간 시황·공시 헤드라인 표본 중 발굴 후보 종목이 언급된 기사 목록입니다.")

    table_data = []
    for h in headlines:
        sym = h.get("symbol", "")
        payload = h.get("payload") or {}
        table_data.append({
            "종목": symbol_label(sym, names),
            "제목": h.get("title", ""),
            "언급된 종목들": ", ".join(symbol_label(s, names) for s in payload.get("matched_symbols", [])),
        })

    df = pd.DataFrame(table_data)
    st.dataframe(df, width="stretch", hide_index=True)


def render_holding_news(holding_news: List[Dict[str, Any]], names: Dict[str, str]) -> None:
    if not holding_news:
        st.info("이 묶음의 집계 구간 동안 새로 수집된 보유 종목 뉴스가 없습니다.")
        return

    st.caption("포트폴리오 보유 종목에 대해 수집되어 감성 분석을 거친 뉴스 목록입니다 (중복 기사는 통합 압축됨).")

    by_sym: Dict[str, List[Dict[str, Any]]] = {}
    for n in holding_news:
        sym = n.get("symbol", "기타")
        by_sym.setdefault(sym, []).append(n)

    for sym, articles in by_sym.items():
        sym_label = symbol_label(sym, names)
        st.markdown(f"##### {sym_label} ({len(articles)}건)")
        for a in articles:
            payload = a.get("payload") or {}
            score = payload.get("score")
            reasoning = payload.get("reasoning", "")
            title = a.get("title", "")
            url = a.get("url")

            score_badge = ""
            if score is not None:
                if score > 0.15:
                    score_badge = f"🟢 BUY (+{score:.2f})"
                elif score < -0.15:
                    score_badge = f"🔴 SELL ({score:.2f})"
                else:
                    score_badge = f"🟡 HOLD ({score:.2f})"

            title_link = f"[{title}]({url})" if url else title
            st.markdown(f"- **{title_link}** `{score_badge}`")
            if reasoning:
                st.caption(f"  └ LLM 판단 근거: {reasoning}")


def render_candidates(candidates: List[Dict[str, Any]], names: Dict[str, str]) -> None:
    if not candidates:
        st.info("발굴 후보 종목 데이터가 없습니다.")
        return

    st.caption("관점별(저평가, 하입 조기신호, 모멘텀, 증권사) 스크리닝 및 복합 점수가 매겨진 후보 풀입니다.")

    rows = []
    for c in candidates:
        sym = c.get("symbol", "")
        payload = c.get("payload") or {}
        sem_val = payload.get("semantic_score")
        sem_pen = payload.get("semantic_penalty")
        sem_str = "-"
        if sem_val is not None:
            sem_str = f"{sem_val:+.2f}"
            if sem_pen:
                sem_str += f" (페널티 -{sem_pen:.2f})"
        elif isinstance(payload.get("components"), dict) and payload["components"].get("semantic") is not None:
            sem_str = f"{payload['components']['semantic']:.2f}"

        rows.append({
            "숏리스트": "✅ 숏리스트" if payload.get("shortlisted") else "-",
            "종목": symbol_label(sym, names),
            "관점": payload.get("angle_label") or c.get("title"),
            "복합점수": f"{payload.get('score', 0):.3f}" if payload.get("score") is not None else "-",
            "AI시맨틱": sem_str,
            "목표가괴리": fmt_pct(payload.get("target_gap_pct")) if payload.get("target_gap_pct") is not None else "-",
            "PER": f"{payload.get('per'):.1f}" if payload.get("per") else "-",
            "뉴스가속": f"{payload.get('news_accel'):.1f}배" if payload.get("news_accel") else "-",
            "거래량5/20": f"{payload.get('vol_ratio_5_20'):.1f}배" if payload.get("vol_ratio_5_20") else "-",
            "5일수익률": f"{payload.get('ret_5d_pct'):+.1f}%" if payload.get("ret_5d_pct") is not None else "-",
            "발굴근거": " | ".join(payload.get("thesis") or []) if payload.get("thesis") else "-",
        })

    df = pd.DataFrame(rows)
    st.dataframe(df, width="stretch", hide_index=True)


def render_symbol_timeline(symbol: str, names: Dict[str, str]) -> None:
    if not symbol:
        st.info("종목을 선택하거나 검색하세요.")
        return

    sym_data = cached_get(f"/insights/symbols/{symbol}", ttl=30, timeout=30)
    if not sym_data:
        st.info(f"{symbol_label(symbol, names)} 종목에 대한 인사이트 기록이 없습니다.")
        return

    st.markdown(f"#### 🔎 {symbol_label(symbol, names)} 인사이트 타임라인 (최근 {len(sym_data)}건)")

    for item in sym_data:
        kind = item.get("kind")
        batch_ts = item.get("batch_started_at")
        time_str = fmt_kst(batch_ts, "%Y-%m-%d %H:%M")
        kind_name = {
            "broker_report": "📑 증권사 리포트",
            "market_headline": "📰 시장 헤드라인",
            "holding_news": "🗞️ 보유종목 뉴스",
            "candidate": "🎯 발굴 후보",
        }.get(kind, kind)

        payload = item.get("payload") or {}
        title = item.get("title", "")

        with st.expander(f"[{time_str}] {kind_name} — {title}"):
            if kind == "broker_report":
                analysis = payload.get("analysis") or {}
                if analysis:
                    st.markdown(f"**판정:** `{analysis.get('stance')}` | **요약:** {analysis.get('summary')}")
                if payload.get("target_price"):
                    st.caption(f"목표주가: {payload.get('target_price'):,.0f}원 (의견: {payload.get('opinion')})")
            elif kind == "holding_news":
                st.caption(f"감성 점수: {payload.get('score')} | 근거: {payload.get('reasoning')}")
            elif kind == "candidate":
                st.markdown(f"**관점:** {payload.get('angle_label')} (점수: {payload.get('score')})")
                st.caption(f"근거: {' | '.join(payload.get('thesis') or [])}")
