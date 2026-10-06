# ui/dividend_tab.py
"""💰 배당·분배금 탭 — 주식 배당금 및 ETF 분배금 내역 관리, 통계, KIS 연동 및 CSV 업로드."""
import io
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from ui import theme
from ui.common import api_delete, api_get, api_post, clear_cache, fmt_krw, fmt_kst, symbol_label, today_kst


def _kpis(summary: Dict[str, Any]) -> None:
    totals = summary.get("totals", {})
    by_month = summary.get("by_month", [])
    by_symbol = summary.get("by_symbol", [])

    this_month_str = today_kst()[:7]
    this_month_net = next((m.get("net_amount_krw") or m.get("total_net_krw", 0.0) for m in by_month if m.get("month") == this_month_str), 0.0)

    top_sym = by_symbol[0]["symbol"] if by_symbol else "-"
    top_sym_amt = (by_symbol[0].get("net_amount_krw") or by_symbol[0].get("total_net_krw", 0.0)) if by_symbol else 0.0

    c = st.columns(5)
    c[0].metric("총 배당금(세후)", fmt_krw(totals.get("total_net_krw", 0)), help="수령 완료된 순 배당/분배금 합계 (KRW)")
    c[1].metric("세전 총액", fmt_krw(totals.get("total_gross_krw", 0)), help="세금 공제 전 배당금 총액")
    c[2].metric("원천징수 세금", fmt_krw(totals.get("total_tax_krw", 0)), help="납부된 소득세/지방소득세 등 세금 총액")
    c[3].metric("이번 달 배당금", fmt_krw(this_month_net), help=f"{this_month_str} 수령액")
    c[4].metric("최대 기여 종목", top_sym, delta=fmt_krw(top_sym_amt) if by_symbol else None, help="가장 많은 배당금을 지급한 종목")


def _chart_monthly(by_month: List[Dict[str, Any]]) -> go.Figure:
    colors = theme.gain_loss()
    months = [m.get("month", "") for m in by_month]
    amounts = [m.get("net_amount_krw") or m.get("total_net_krw", 0.0) for m in by_month]

    fig = go.Figure(go.Bar(
        x=months, y=amounts,
        marker=dict(color=colors["gain"]),
        text=[f"{v:,.0f}원" for v in amounts],
        textposition="outside",
        cliponaxis=False,
        hovertemplate="%{x}<br>%{y:,.0f}원<extra></extra>",
    ))
    fig.update_layout(title="월별 배당·분배금 수령 추이", bargap=0.4)
    fig.update_yaxes(ticksuffix="원", showgrid=True, gridcolor=theme.tokens()["grid"])
    fig.update_xaxes(type="category", showgrid=False)
    return theme.style(fig, height=320, legend=False)


def _chart_by_symbol(by_symbol: List[Dict[str, Any]], names: Dict[str, str]) -> go.Figure:
    labels = [symbol_label(s["symbol"], names) for s in by_symbol]
    values = [s.get("net_amount_krw") or s.get("total_net_krw", 0.0) for s in by_symbol]

    fig = px.pie(
        names=labels, values=values,
        hole=0.45,
        title="종목별 배당금 비중",
    )
    fig.update_traces(textposition="inside", textinfo="percent+label", hovertemplate="%{label}: %{value:,.0f}원 (%{percent})<extra></extra>")
    return theme.style(fig, height=320)


def render(names: Dict[str, str]) -> None:
    st.markdown("### 💰 배당 및 ETF 분배금 관리")
    st.caption("주식 배당금 및 ETF 분배금을 기록하고 계좌의 총수익(Total Return)에 반영합니다.")

    # 1. 요약 데이터 조회
    summary = api_get("/dividends/summary", timeout=10) or {}
    _kpis(summary)

    # 2. 차트 영역
    by_month = summary.get("by_month", [])
    by_symbol = summary.get("by_symbol", [])

    if by_month or by_symbol:
        col_m, col_s = st.columns([3, 2])
        with col_m:
            if by_month:
                st.plotly_chart(_chart_monthly(by_month), width="stretch")
            else:
                st.info("월별 수령 내역이 없습니다.")
        with col_s:
            if by_symbol:
                st.plotly_chart(_chart_by_symbol(by_symbol, names), width="stretch")
            else:
                st.info("종목별 내역이 없습니다.")

    st.divider()

    # 3. 배당금 등록 / 연동 도구
    with st.expander("➕ 배당금 등록 및 KIS 계좌 연동 도구", expanded=False):
        t1, t2, t3 = st.tabs(["직접 등록", "KIS 계좌 동기화", "CSV 일괄 업로드"])

        with t1:
            st.markdown("##### ✍️ 배당·분배금 직접 입력")
            with st.form("manual_div_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                f_sym = c1.text_input("종목코드 *", placeholder="예: 005930").strip()
                f_market = c2.selectbox("시장", ["domestic", "overseas"], format_func=lambda x: "국내" if x == "domestic" else "해외")
                f_type = c3.selectbox("유형", ["CASH", "ETF_DIST", "STOCK"], format_func=lambda x: {"CASH": "현금배당", "ETF_DIST": "ETF 분배금", "STOCK": "주식배당"}.get(x, x))

                c4, c5 = st.columns(2)
                f_rec_date = c4.text_input("배당기준일 (선택, YYYY-MM-DD)", placeholder="예: 2026-12-31")
                f_pay_date = c5.text_input("지급일 * (YYYY-MM-DD)", value=today_kst(), placeholder="예: 2026-04-15")

                c6, c7, c8 = st.columns(3)
                f_qty = c6.number_input("보유 수량", min_value=0.0, value=0.0, step=1.0)
                f_dps = c7.number_input("주당 배당금 (선택)", min_value=0.0, value=0.0, step=10.0)
                f_gross = c8.number_input("세전 총액 (선택)", min_value=0.0, value=0.0, step=1000.0)

                c9, c10, c11 = st.columns(3)
                f_tax = c9.number_input("원천징수세액 (선택)", min_value=0.0, value=0.0, step=100.0)
                f_net = c10.number_input("실수령액 (세후) *", min_value=0.0, value=0.0, step=1000.0)
                f_curr = c11.selectbox("통화", ["KRW", "USD"])

                f_notes = st.text_input("메모", placeholder="예: 2025 결산배당")

                sub = st.form_submit_button("배당금 저장", width="stretch")
                if sub:
                    if not f_sym:
                        st.error("종목코드를 입력하세요.")
                    elif not f_pay_date:
                        st.error("지급일을 입력하세요.")
                    elif f_net <= 0 and f_gross <= 0:
                        st.error("실수령액 또는 세전 총액을 입력하세요.")
                    else:
                        payload = {
                            "symbol": f_sym,
                            "market": f_market,
                            "dividend_type": f_type,
                            "record_date": f_rec_date or None,
                            "payment_date": f_pay_date,
                            "qty": f_qty if f_qty > 0 else None,
                            "dps": f_dps if f_dps > 0 else None,
                            "gross_amount": f_gross if f_gross > 0 else None,
                            "tax_amount": f_tax if f_tax > 0 else None,
                            "net_amount": f_net if f_net > 0 else (f_gross - f_tax),
                            "currency": f_curr,
                            "notes": f_notes or None,
                        }
                        res = api_post("/dividends", json=payload)
                        if res:
                            st.success(f"{f_sym} 배당금 {f_net:,.0f}원이 등록되었습니다.")
                            clear_cache()
                            st.rerun()

        with t2:
            st.markdown("##### 🔄 KIS OpenAPI 계좌 권리조회 동기화")
            st.caption("한국투자증권 API(CTRGA011R)를 통해 계좌에 입금된 배당 및 ETF 분배금 내역을 자동 수집합니다.")
            sc1, sc2, sc3 = st.columns([2, 2, 1])
            def_start = f"{int(today_kst()[:4])-1}-01-01"
            sync_start = sc1.text_input("조회 시작일", value=def_start)
            sync_end = sc2.text_input("조회 종료일", value=today_kst())
            sync_btn = sc3.button("지금 동기화", width="stretch", help="KIS API에서 배당 내역을 가져옵니다.")
            if sync_btn:
                with st.spinner("KIS API로부터 배당 내역을 수집하는 중..."):
                    res = api_post("/dividends/sync", params={"start_date": sync_start.replace("-", ""), "end_date": sync_end.replace("-", "")})
                    if res:
                        st.success(f"동기화 완료: 총 {res.get('total_fetched', 0)}건 조회 중 {res.get('inserted_count', 0)}건 신규 반영 (중복 {res.get('skipped_count', 0)}건 건너뜀)")
                        clear_cache()
                        st.rerun()
                    else:
                        st.error("동기화에 실패했습니다. 백엔드 API 서버 상태 및 계좌 연동을 확인하세요 (모의투자는 권리조회를 지원하지 않을 수 있습니다).")

        with t3:
            st.markdown("##### 📁 CSV 파일 일괄 등록")
            st.caption("증권사 HTS/MTS에서 다운로드한 배당 내역 CSV를 업로드하여 일괄 등록합니다.")
            st.markdown("""
            **필수 헤더 예시**: `symbol, payment_date, net_amount`  
            *선택 헤더*: `dividend_type, record_date, qty, dps, gross_amount, tax_amount, currency, notes`
            """)
            uploaded = st.file_uploader("CSV 파일 선택", type=["csv"])
            if uploaded:
                try:
                    df_preview = pd.read_csv(uploaded)
                    st.dataframe(df_preview.head(5), width="stretch")
                    if st.button("CSV 일괄 저장", key="btn_csv_upload"):
                        uploaded.seek(0)
                        csv_text = uploaded.getvalue().decode("utf-8-sig")
                        res = api_post("/dividends/import-csv", data=csv_text.encode("utf-8"), headers={"Content-Type": "text/plain"})
                        if res:
                            st.success(f"CSV 등록 완료: {res.get('inserted_count', 0)}건 추가되었습니다.")
                            clear_cache()
                            st.rerun()
                        else:
                            st.error("CSV 일괄 등록에 실패했습니다. 데이터 형식 및 API 서버 상태를 확인하세요.")
                except Exception as e:
                    st.error(f"CSV 파싱 오류: {e}")

    # 4. 전체 배당 내역 테이블
    st.markdown("#### 📋 전체 배당 및 분배금 내역")
    div_list = api_get("/dividends", timeout=10) or []
    if not div_list:
        st.info("등록된 배당·분배금 내역이 없습니다. 위 등록 도구로 추가해보세요.")
        return

    table_data = []
    for d in div_list:
        table_data.append({
            "ID": d["id"],
            "지급일": d["payment_date"],
            "종목": symbol_label(d["symbol"], names),
            "유형": {"CASH": "현금배당", "ETF_DIST": "ETF 분배", "STOCK": "주식배당"}.get(d["dividend_type"], d["dividend_type"]),
            "수량": d.get("qty"),
            "주당금액": d.get("dps"),
            "세전금액": d.get("gross_amount"),
            "세금": d.get("tax_amount"),
            "실수령액(KRW)": d.get("net_amount_krw") or d.get("net_amount"),
            "통화": d.get("currency", "KRW"),
            "출처": d.get("source", "MANUAL"),
            "메모": d.get("notes") or "",
        })

    df = pd.DataFrame(table_data)
    st.dataframe(
        df, width="stretch", hide_index=True,
        column_config={
            "ID": st.column_config.NumberColumn(format="%d"),
            "수량": st.column_config.NumberColumn(format="%g"),
            "주당금액": st.column_config.NumberColumn(format="%,.2f"),
            "세전금액": st.column_config.NumberColumn(format="%,.0f"),
            "세금": st.column_config.NumberColumn(format="%,.0f"),
            "실수령액(KRW)": st.column_config.NumberColumn(format="%,.0f"),
        }
    )

    # 개별 삭제 옵션
    with st.expander("🗑️ 배당 내역 삭제"):
        del_c1, del_c2 = st.columns([3, 1])
        del_id = del_c1.selectbox("삭제할 배당 내역 선택 (ID / 종목 / 지급일)", div_list, format_func=lambda x: f"ID {x['id']} - {x['symbol']} ({x['payment_date']}) 실수령 {x.get('net_amount_krw') or x.get('net_amount'):,.0f}원")
        if del_c2.button("삭제 실행", type="primary", width="stretch"):
            if del_id:
                res = api_delete(f"/dividends/{del_id['id']}")
                if res:
                    st.success(f"ID {del_id['id']} 배당 내역이 삭제되었습니다.")
                    clear_cache()
                    st.rerun()
