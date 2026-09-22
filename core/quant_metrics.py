# core/quant_metrics.py
"""표준 성과/리스크 지표. core/indicators.py와 마찬가지로 이미 조회한 OHLCV
DataFrame(chart_rows_to_dataframe 결과)을 입력받아 계산하므로 종목당 추가 KIS 호출이
없다 - 기술적 시그널 계산과 같은 데이터를 재사용한다.

여기 있는 지표는 signal_engine의 매수/매도 판단에는 쓰이지 않는다(그건 여전히
indicators.py의 몫) - AI 포트폴리오 에이전트가 보유/후보 종목의 위험·수익 특성을
파악하는 컨텍스트로만 쓰인다.

승률/수익계수/손익비/계좌 전체 회전율은 의도적으로 빠져 있다 - 실제 체결 이력을
왕복매매(진입→청산) 단위로 재구성해야 하는데, 라이브 계좌의 거래 이력이 아직 거의
없어(페이퍼트레이딩 시작 직후) 지금 만들어도 표본이 너무 작아 무의미하기 때문이다.
"""
import logging
import time
from typing import Any, Dict, Optional

import pandas as pd

from core import indicators, kis_domestic
from core.config import settings
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
MIN_BARS_FOR_RATIOS = 30  # 표준편차/베타 등은 최소 이 정도는 있어야 의미가 있음
MIN_DAYS_HELD_FOR_CAGR = 7  # 1주 미만 보유를 연율화하면 노이즈가 지나치게 증폭됨

# 코스피 지수 자체를 조회하는 API 대신, 이미 쓰고 있는 get_daily_chart로 그대로 조회
# 가능한 KODEX 200(069500) ETF를 시장 벤치마크 프록시로 쓴다 - 새 API 연동 불필요.
BENCHMARK_SYMBOL = "069500"


def compute_price_based_metrics(
    df: pd.DataFrame, benchmark_returns: Optional[pd.Series] = None
) -> Dict[str, Any]:
    """일봉 df 하나로 계산 가능한 것들을 한 번에 반환한다. 데이터가 짧으면 계산 가능한
    것만 채우고 나머지는 생략한다(전부 강제로 채우려다 통계적으로 무의미한 값을 만들지
    않기 위함)."""
    if df.empty or len(df) < 5:
        return {}

    close = df["close"]
    result: Dict[str, Any] = {"period_return_pct": float((close.iloc[-1] / close.iloc[0] - 1) * 100)}

    running_max = close.cummax()
    drawdown = (close - running_max) / running_max
    result["mdd_pct"] = float(drawdown.min() * 100)  # 음수 (예: -12.3)

    returns = close.pct_change().dropna()
    if len(returns) < MIN_BARS_FOR_RATIOS:
        return result

    annual_factor = TRADING_DAYS_PER_YEAR ** 0.5
    vol = returns.std()
    result["volatility_pct"] = float(vol * annual_factor * 100)

    daily_rf = settings.RISK_FREE_RATE_ANNUAL / TRADING_DAYS_PER_YEAR
    excess_mean = returns.mean() - daily_rf
    if vol > 0:
        result["sharpe"] = float((excess_mean / vol) * annual_factor)

    downside = returns[returns < 0]
    downside_std = downside.std() if len(downside) > 1 else 0.0
    if downside_std and downside_std > 0:
        result["sortino"] = float((excess_mean / downside_std) * annual_factor)

    if benchmark_returns is not None and len(benchmark_returns) >= MIN_BARS_FOR_RATIOS:
        aligned = pd.concat([returns, benchmark_returns], axis=1, join="inner").dropna()
        if len(aligned) >= MIN_BARS_FOR_RATIOS:
            bench_var = aligned.iloc[:, 1].var()
            if bench_var > 0:
                cov = aligned.iloc[:, 0].cov(aligned.iloc[:, 1])
                result["beta"] = float(cov / bench_var)

    return result


def compute_position_return(
    avg_price: float, current_price: float, entry_opened_at: Optional[float]
) -> Dict[str, Any]:
    """보유 포지션의 내 평단가 기준 ROI/CAGR. compute_price_based_metrics는 종목 자체의
    시장 성과를 보므로, 이 함수와는 값이 다를 수 있다(예: 저점에서 진입했으면 종목의
    최근 6개월 수익률보다 내 ROI가 더 좋을 수 있음) - 둘 다 LLM에 넘겨 구분해서 보여준다."""
    if avg_price <= 0 or current_price <= 0:
        return {}
    result: Dict[str, Any] = {"roi_pct": float((current_price / avg_price - 1) * 100)}
    if entry_opened_at:
        days_held = max((time.time() - entry_opened_at) / 86400, 0)
        result["days_held"] = round(days_held, 1)
        if days_held >= MIN_DAYS_HELD_FOR_CAGR:
            years = days_held / 365
            try:
                result["cagr_pct"] = float(((current_price / avg_price) ** (1 / years) - 1) * 100)
            except (ZeroDivisionError, OverflowError, ValueError):
                pass
    return result


async def fetch_benchmark_return_series(
    client: AsyncKISClient, lookback_days: int = 90
) -> Optional[pd.Series]:
    """베타 계산용 벤치마크 일간수익률 시계열을 한 번만 조회한다 (호출부가 캐싱/재사용
    책임을 진다 - 종목마다 다시 조회하면 그만큼 KIS 호출이 늘어난다)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(tz=ZoneInfo("Asia/Seoul"))
    start_date = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")
    try:
        chart_rows = await kis_domestic.get_daily_chart(client, BENCHMARK_SYMBOL, start_date, end_date)
        df = indicators.chart_rows_to_dataframe(chart_rows)
        if df.empty:
            return None
        return df["close"].pct_change().dropna()
    except Exception:
        logger.exception("벤치마크(KODEX 200) 시계열 조회 실패 - 베타 계산 없이 계속")
        return None
