# ui/backtest_tab.py
"""전략 백테스팅 대시보드 탭.

과거 데이터에 Finance Jarvis의 실제 매매 전략(손절 -7%, ATR 트레일링 익절,
스윙 시그널, 온보딩, 수수료/거래세/슬리피지)을 시뮬레이션하고,
동기간 S&P 500 ETF(360750 TIGER 미국S&P500) 베이스라인과 비교 검증합니다.
"""
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from ui.common import API_BASE, fmt_krw, symbol_label


def render(names: Optional[Dict[str, str]] = None) -> None:
    st.subheader("🧪 Finance Jarvis 전략 백테스팅")
    st.caption(
        "Finance Jarvis의 실제 매매 엔진(손절 -7%, ATR 트레일링 익절, RSI/MACD/BBands 스윙 시그널, "
        "밴드 상한 강제축소, 온보딩 분할매수)을 과거 일봉에 시뮬레이션하고, "
        "동기간 **S&P 500 ETF(360750 TIGER 미국S&P500)** 베이스라인과 1:1 비교 검증합니다."
    )

    # 1. 제어 파라미터 입력 영역
    with st.expander("⚙️ 백테스트 파라미터 설정", expanded=True):
        col_sym, col_bench = st.columns([3, 2])
        with col_sym:
            default_symbols = "005930,000660"
            symbols_input = st.text_input(
                "테스트 대상 종목코드 (쉼표 구분)",
                value=default_symbols,
                help="예: 005930,000660,017670 (삼성전자, SK하이닉스, SK텔레콤 등)",
            )
        with col_bench:
            bench_input = st.text_input(
                "베이스라인 벤치마크 (S&P 500 ETF)",
                value="360750",
                help="기본값: 360750 (TIGER 미국S&P500 - 원화 계좌 1:1 비교용 대장주 ETF)",
            )

        col_period, col_dates = st.columns([2, 3])
        with col_period:
            period_preset = st.radio(
                "테스트 기간 선택",
                options=["최근 1개월", "최근 3개월", "최근 6개월", "최근 1년", "직접 입력"],
                index=2,
                horizontal=True,
            )

        today = datetime.now().date()
        if period_preset == "최근 1개월":
            start_default = today - timedelta(days=30)
            end_default = today
        elif period_preset == "최근 3개월":
            start_default = today - timedelta(days=90)
            end_default = today
        elif period_preset == "최근 6개월":
            start_default = today - timedelta(days=180)
            end_default = today
        elif period_preset == "최근 1년":
            start_default = today - timedelta(days=365)
            end_default = today
        else:
            start_default = today - timedelta(days=180)
            end_default = today

        with col_dates:
            d_col1, d_col2 = st.columns(2)
            start_date = d_col1.date_input("시작일", value=start_default, max_value=today)
            end_date = d_col2.date_input("종료일", value=end_default, max_value=today)

        col_c1, col_c2, col_c3, col_c4 = st.columns(4)
        with col_c1:
            initial_cash = st.number_input(
                "초기 자본금 (원)",
                value=10_000_000,
                step=1_000_000,
                format="%d",
            )
        with col_c2:
            stop_loss_pct = st.number_input(
                "손절 기준 (STOP_LOSS)",
                value=0.06,
                step=0.01,
                format="%.2f",
                help="평단가 대비 -6% 도달 시 즉시 전량 매도 (최적화값)",
            )
        with col_c3:
            cooldown_days = st.number_input(
                "손절 후 쿨다운 (일)",
                value=5,
                step=1,
                min_value=0,
                max_value=30,
                help="손절 발생 후 동일 종목 재진입 방지 쿨다운 기간 (역추세 연속 손절 차단, 기본 5일)",
            )
        with col_c4:
            onboard_days = st.number_input(
                "분할 온보딩 (일)",
                value=3,
                step=1,
                min_value=1,
                max_value=10,
                help="신규/저비중 종목 편입 시 목표비중 분할 매수 일수 (기본 3일)",
            )

        col_f1, col_f2 = st.columns(2)
        with col_f1:
            require_uptrend = st.checkbox(
                "중기 상승 추세 필터 (SMA20 >= SMA60 시에만 온보딩)",
                value=True,
                help="하락 추세 종목의 기계적 물타기를 차단하고 상승 추세 종목에만 자본을 배분합니다.",
            )
        with col_f2:
            require_sma20 = st.checkbox(
                "단기 이평선 필터 (현재가 >= SMA20 시에만 매수)",
                value=True,
                help="단기 급락 중인 종목의 칼날잡기를 차단합니다.",
            )

        run_btn = st.button("🚀 백테스트 실행", type="primary", use_container_width=True)

    if run_btn:
        symbols_list = [s.strip() for s in symbols_input.split(",") if s.strip()]
        if not symbols_list:
            st.error("테스트 대상 종목을 최소 1개 이상 입력해주세요.")
            return

        payload = {
            "symbols": symbols_list,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "initial_cash": float(initial_cash),
            "benchmark_symbol": bench_input.strip() or "360750",
            "fee_rate": 0.00015,
            "tax_rate": 0.0018,
            "slippage_pct": 0.0005,
            "stop_loss_pct": float(stop_loss_pct),
            "stop_loss_cooldown_days": int(cooldown_days),
            "onboarding_days": int(onboard_days),
            "require_uptrend_for_onboarding": bool(require_uptrend),
            "require_sma20_for_buy": bool(require_sma20),
        }

        with st.spinner("과거 데이터 수집 및 퀀트 백테스트 시뮬레이션 중..."):
            try:
                resp = requests.post(f"{API_BASE}/backtest/run", json=payload, timeout=60)
                if resp.status_code != 200:
                    st.error(f"백테스트 실행 실패: {resp.text}")
                    return
                res = resp.json()
            except Exception as e:
                st.error(f"API 요청 실패: {e}")
                return

        st.session_state["_backtest_last_result"] = res

    res = st.session_state.get("_backtest_last_result")
    if not res:
        st.info("상단에서 종목과 기간을 선택하고 [🚀 백테스트 실행] 버튼을 눌러주세요.")
        return

    metrics = res.get("metrics", {})
    strat = metrics.get("strategy", {})
    bench = metrics.get("benchmark", {})
    rel = metrics.get("relative", {})
    trades = metrics.get("trade_stats", {})
    chart_data = res.get("chart_data", [])
    trade_list = res.get("trades", [])

    # 2. 핵심 KPI 메트릭 카드
    st.markdown("### 📊 성과 및 벤치마크(S&P 500 ETF) 비교 요약")
    
    kpi_col1, kpi_col2, kpi_col3, kpi_col4 = st.columns(4)
    strat_ret = strat.get("total_return_pct", 0.0)
    bench_ret = bench.get("benchmark_return_pct", 0.0)
    diff_ret = rel.get("excess_return_pct", 0.0)
    kpi_col1.metric(
        "누적 수익률 (Total Return)",
        f"{strat_ret:+.2f}%",
        delta=f"{diff_ret:+.2f}%p vs S&P 500 ({bench_ret:+.2f}%)",
    )

    strat_cagr = strat.get("cagr_pct", 0.0)
    bench_cagr = bench.get("benchmark_cagr_pct", 0.0)
    kpi_col2.metric(
        "연평균 복리 (CAGR)",
        f"{strat_cagr:+.2f}%",
        delta=f"{strat_cagr - bench_cagr:+.2f}%p vs S&P 500 ({bench_cagr:+.2f}%)",
    )

    strat_mdd = strat.get("mdd_pct", 0.0)
    bench_mdd = bench.get("benchmark_mdd_pct", 0.0)
    kpi_col3.metric(
        "최대 낙폭 (MDD)",
        f"{strat_mdd:.2f}%",
        delta=f"{strat_mdd - bench_mdd:+.2f}%p vs S&P 500 ({bench_mdd:.2f}%)",
        delta_color="inverse",
    )

    strat_sharpe = strat.get("sharpe", 0.0)
    bench_sharpe = bench.get("benchmark_sharpe", 0.0)
    kpi_col4.metric(
        "샤프 지수 (Sharpe)",
        f"{strat_sharpe:.2f}",
        delta=f"{strat_sharpe - bench_sharpe:+.2f} vs S&P 500 ({bench_sharpe:.2f})",
    )

    sub_col1, sub_col2, sub_col3, sub_col4 = st.columns(4)
    beta_val = rel.get("beta")
    sub_col1.metric("베타 (Beta vs S&P 500)", f"{beta_val:.2f}" if beta_val is not None else "N/A")
    alpha_val = rel.get("alpha_pct")
    sub_col2.metric("젠센의 알파 (Alpha)", f"{alpha_val:+.2f}%" if alpha_val is not None else "N/A")
    win_rate = trades.get("win_rate_pct", 0.0)
    sub_col3.metric("매매 승률 (Win Rate)", f"{win_rate:.1f}%", f"{trades.get('win_count', 0)}승 / {trades.get('loss_count', 0)}패")
    profit_factor = trades.get("profit_factor", 0.0)
    sub_col4.metric("손익비 (Profit Factor)", f"{profit_factor:.2f}")

    # 리스크 및 안전장치 통계
    st.markdown(
        f"""
        <div style="background: rgba(127,127,127,0.08); border-radius: 10px; padding: 12px 18px; margin: 15px 0;">
            <b>🛡️ 안전장치 및 비용 통계:</b>
            손절(-7% Stop Loss) 발동: <b>{trades.get('stop_loss_count', 0)}회</b> · 
            ATR 트레일링 익절 발동: <b>{trades.get('trailing_stop_count', 0)}회</b> · 
            총 체결: <b>{trades.get('total_trades', 0)}건</b> (매수 {trades.get('buy_trades', 0)} / 매도 {trades.get('sell_trades', 0)}) · 
            수수료 및 거래세: <b>{trades.get('total_fees_and_taxes', 0.0):,.0f}원</b> · 
            최종 자산: <b>{strat.get('final_equity', 0.0):,.0f}원</b>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 3. Plotly 차트 시각화
    if chart_data:
        cdf = pd.DataFrame(chart_data)
        
        st.markdown("#### 📈 자산 성장 곡선: Jarvis 전략 vs S&P 500 ETF")
        fig_equity = go.Figure()
        fig_equity.add_trace(
            go.Scatter(
                x=cdf["date"],
                y=cdf["strategy_equity"],
                name="Jarvis 전략 자산",
                line=dict(color="#1f77b4", width=2.5),
            )
        )
        if "benchmark_equity" in cdf and cdf["benchmark_equity"].notna().any():
            fig_equity.add_trace(
                go.Scatter(
                    x=cdf["date"],
                    y=cdf["benchmark_equity"],
                    name=f"S&P 500 ETF ({res['config'].get('benchmark_symbol', '360750')})",
                    line=dict(color="#ff7f0e", width=2, dash="dash"),
                )
            )
        fig_equity.add_trace(
            go.Scatter(
                x=cdf["date"],
                y=cdf["cash"],
                name="보유 현금 (Cash)",
                line=dict(color="#2ca02c", width=1.5, dash="dot"),
            )
        )
        fig_equity.update_layout(
            hovermode="x unified",
            xaxis_title="일자",
            yaxis_title="평가 자산 (원)",
            margin=dict(l=20, r=20, t=30, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig_equity, use_container_width=True)

        st.markdown("#### 🌊 언더워터 낙폭 차트 (Drawdown %)")
        fig_dd = go.Figure()
        fig_dd.add_trace(
            go.Scatter(
                x=cdf["date"],
                y=cdf["strategy_drawdown"],
                name="Jarvis 전략 낙폭 (%)",
                line=dict(color="#d62728", width=1.8),
                fill="tozeroy",
                fillcolor="rgba(214, 39, 40, 0.15)",
            )
        )
        if "benchmark_drawdown" in cdf and cdf["benchmark_drawdown"].notna().any():
            fig_dd.add_trace(
                go.Scatter(
                    x=cdf["date"],
                    y=cdf["benchmark_drawdown"],
                    name="S&P 500 ETF 낙폭 (%)",
                    line=dict(color="#ff7f0e", width=1.5, dash="dash"),
                )
            )
        fig_dd.update_layout(
            hovermode="x unified",
            xaxis_title="일자",
            yaxis_title="고점 대비 낙폭 (%)",
            margin=dict(l=20, r=20, t=30, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig_dd, use_container_width=True)

    # 4. 상세 매매 일지
    st.markdown("#### 📜 시뮬레이션 매매 일지")
    if trade_list:
        tdf = pd.DataFrame(trade_list)
        tdf["종목"] = tdf["symbol"].apply(lambda s: symbol_label(s, names))
        tdf["구분"] = tdf["side"].apply(lambda s: "🟢 매수" if s == "BUY" else "🔴 매도")
        tdf["체결가"] = tdf["price"].apply(lambda p: f"{p:,.0f}원")
        tdf["수량"] = tdf["qty"].apply(lambda q: f"{q:,}주")
        tdf["체결금액"] = tdf["notional"].apply(lambda n: f"{n:,.0f}원")
        tdf["수수료/세금"] = (tdf["fee"] + tdf["tax"]).apply(lambda c: f"{c:,.0f}원")
        tdf["실현손익"] = tdf["realized_pnl"].apply(lambda p: f"{p:+,.0f}원" if p != 0 else "-")
        tdf["수익률"] = tdf["return_pct"].apply(lambda r: f"{r:+.2f}%" if r != 0 else "-")

        display_cols = [
            "date", "종목", "구분", "reason", "체결가", "수량", "체결금액", "수수료/세금", "실현손익", "수익률"
        ]
        col_rename = {
            "date": "일자",
            "reason": "매매 사유",
        }
        st.dataframe(
            tdf[display_cols].rename(columns=col_rename),
            use_container_width=True,
            hide_index=True,
            height=320,
        )
    else:
        st.caption("해당 기간 동안 발생한 체결 내역이 없습니다.")
