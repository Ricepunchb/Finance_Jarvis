# core/indicators.py
"""기술적 지표 기반 매매 시그널. pandas-ta로 계산하며 수식을 직접 구현하지 않는다
(단, CCI는 예외 - pandas_ta 3.0.6의 cci() 자체에 버그가 있어 직접 계산한다. 아래
_compute_cci 참고)."""
import math
from typing import Any, Dict, List

import pandas as pd
import pandas_ta as ta

MIN_BARS_REQUIRED = 30
CCI_COLUMN = "CCI_20"


def _first_matching_column(df: pd.DataFrame, prefix: str) -> str:
    for col in df.columns:
        if col.startswith(prefix):
            return col
    raise KeyError(f"'{prefix}'로 시작하는 컬럼을 찾을 수 없습니다: {list(df.columns)}")


def _compute_cci(df: pd.DataFrame, length: int = 20, c: float = 0.015) -> pd.Series:
    """CCI = (전형가 - 전형가의 이동평균) / (c * 전형가의 평균절대편차).

    pandas_ta 3.0.6의 cci()는 괄호 누락으로 이 식이 아니라
    "전형가 - 이동평균/(c*평균절대편차)"를 계산해 터무니없는 값을 낸다(실측 검증됨,
    수동 계산 대비 수백~수천 배 차이) - 그래서 여기서만 표준 공식대로 직접 계산한다."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = typical_price.rolling(length).mean()
    mean_deviation = (typical_price - sma_tp).abs().rolling(length).mean()
    return (typical_price - sma_tp) / (c * mean_deviation)


def chart_rows_to_dataframe(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """KIS inquire-daily-itemchartprice의 output2 리스트를 OHLCV DataFrame으로 변환.

    KIS는 최신 날짜가 먼저 오도록 반환하므로 시간 순으로 뒤집어준다.
    """
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(
        columns={
            "stck_bsop_date": "date",
            "stck_oprc": "open",
            "stck_hgpr": "high",
            "stck_lwpr": "low",
            "stck_clpr": "close",
            "acml_vol": "volume",
        }
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def chart_rows_to_dataframe_overseas(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """KIS 해외주식 dailyprice의 output2 리스트를 OHLCV DataFrame으로 변환.

    국내(inquire-daily-itemchartprice)와 필드명이 다르다(xymd/open/high/low/clos/tvol).
    """
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(
        columns={"xymd": "date", "open": "open", "high": "high", "low": "low", "clos": "close", "tvol": "volume"}
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def compute_technical_signal(df: pd.DataFrame) -> Dict[str, Any]:
    """RSI/MACD/볼린저밴드를 종합해 -1.0(강한 매도)~+1.0(강한 매수) 점수를 낸다.

    세 지표 모두 이분법 투표가 아니라 연속값으로 -1.0~+1.0 사이를 매끄럽게 움직이는
    "부분 투표"를 반환한다 (과거 30/70·밴드터치 임계값은 그대로 "완전 투표(±1.0)"
    지점으로 유지하고, 그 안쪽 구간을 선형/비선형으로 보간한다). 그렇지 않으면
    RSI·BB가 평상시(중립 구간) 거의 항상 정확히 0.0표를 던지고 MACD만 항상 ±1.0표를
    던져 score가 {0, ±1/3, ±2/3, ±1}로만 양자화되고, strength가 0.333에 쏠린다.

    LLM 결과와 동일한 형태({"direction", "strength", "detail"})로 반환해
    signal_engine이 두 신호를 같은 방식으로 합칠 수 있게 한다.
    """
    if len(df) < MIN_BARS_REQUIRED:
        return {"direction": "HOLD", "strength": 0.0, "detail": "데이터 부족"}

    work = df.copy()
    work.ta.rsi(length=14, append=True)
    work.ta.macd(fast=12, slow=26, signal=9, append=True)
    work.ta.bbands(length=20, std=2, append=True)
    last = work.iloc[-1]

    votes: List[float] = []

    rsi = last[_first_matching_column(work, "RSI_")]
    if pd.notna(rsi):
        # 50=중립, 30/70에서 과거와 동일하게 ±1.0로 포화되는 선형 보간
        vote_rsi = (50.0 - rsi) / 20.0
        votes.append(max(-1.0, min(1.0, vote_rsi)))

    macd = last[_first_matching_column(work, "MACD_")]
    macd_signal = last[_first_matching_column(work, "MACDs_")]
    if pd.notna(macd) and pd.notna(macd_signal):
        hist_col = _first_matching_column(work, "MACDh_")
        last_hist = last[hist_col]
        hist_std = work[hist_col].tail(20).std()
        if pd.notna(hist_std) and hist_std > 1e-9 and pd.notna(last_hist):
            # 히스토그램을 최근 20봉 변동성으로 정규화한 z-score를 tanh로
            # [-1, 1]에 매끄럽게 매핑 (크로스 방향뿐 아니라 강도까지 반영)
            vote_macd = math.tanh(last_hist / hist_std)
        else:
            # 변동성을 추정할 데이터가 부족하면 기존처럼 부호만 사용
            vote_macd = 1.0 if macd > macd_signal else -1.0
        votes.append(vote_macd)

    bb_lower = last[_first_matching_column(work, "BBL_")]
    bb_upper = last[_first_matching_column(work, "BBU_")]
    close = last["close"]
    if pd.notna(bb_lower) and pd.notna(bb_upper) and bb_upper > bb_lower:
        # %B: 0=하단밴드, 0.5=중심선, 1=상단밴드. 하단 터치 +1.0, 상단 터치 -1.0로 선형 매핑
        percent_b = (close - bb_lower) / (bb_upper - bb_lower)
        vote_bb = 1.0 - 2.0 * percent_b
        votes.append(max(-1.0, min(1.0, vote_bb)))

    score = sum(votes) / len(votes) if votes else 0.0
    direction = "BUY" if score > 0.15 else "SELL" if score < -0.15 else "HOLD"
    return {"direction": direction, "strength": min(1.0, abs(score)), "detail": f"score={score:.2f}"}


def compute_technical_detail(df: pd.DataFrame) -> Dict[str, Any]:
    """RSI/MACD/CCI/볼린저밴드의 원시 수치를 그대로 반환한다.

    compute_technical_signal과는 완전히 독립된 함수다 — 일부러 계산을 공유하지 않는다.
    이 함수는 signal_engine의 실거래 판단(compute_technical_signal)에는 전혀 쓰이지
    않고, AI 포트폴리오 에이전트가 참고할 원시 컨텍스트로만 쓰인다. 공유 헬퍼로 묶으면
    리팩터링 실수 하나가 실거래 경로까지 건드릴 위험이 생기므로, 약간의 중복 계산을
    감수하고 분리를 유지한다(90개 안팎의 행이라 계산 비용 자체는 무시할 수준).
    """
    if len(df) < MIN_BARS_REQUIRED:
        return {}

    work = df.copy()
    work.ta.rsi(length=14, append=True)
    work.ta.macd(fast=12, slow=26, signal=9, append=True)
    work.ta.bbands(length=20, std=2, append=True)
    # pandas_ta 3.0.6의 cci()는 소스 코드에 괄호가 빠져 있다: 올바른 식은
    # (typical_price - sma(typical_price)) / (c * mad(typical_price))인데 실제 코드는
    # typical_price - sma(typical_price) / (c * mad(typical_price))로 계산돼 터무니없이
    # 큰 값이 나온다 (실측 검증됨). RSI/MACD/BB는 수동 계산과 정확히 일치함을 확인했으니
    # 이 세 개는 그대로 쓰고, CCI만 직접 계산한다.
    work[CCI_COLUMN] = _compute_cci(work, length=20)
    last = work.iloc[-1]

    detail: Dict[str, Any] = {}

    try:
        rsi = last[_first_matching_column(work, "RSI_")]
        if pd.notna(rsi):
            detail["rsi"] = round(float(rsi), 1)  # <30 과매도(매수관점), >70 과매수
    except KeyError:
        pass

    try:
        macd = last[_first_matching_column(work, "MACD_")]
        macd_signal = last[_first_matching_column(work, "MACDs_")]
        if pd.notna(macd) and pd.notna(macd_signal):
            detail["macd"] = round(float(macd), 2)
            detail["macd_signal"] = round(float(macd_signal), 2)
            detail["macd_cross"] = "golden" if macd > macd_signal else "dead"
    except KeyError:
        pass

    cci = last[CCI_COLUMN]
    if pd.notna(cci):
        detail["cci"] = round(float(cci), 1)  # >100 과매수/강한상승, <-100 과매도/강한하락

    try:
        bb_lower = last[_first_matching_column(work, "BBL_")]
        bb_upper = last[_first_matching_column(work, "BBU_")]
        close = last["close"]
        if pd.notna(bb_lower) and pd.notna(bb_upper) and bb_upper > bb_lower:
            # %B: 0=하단 밴드, 0.5=중단(이동평균), 1=상단 밴드
            detail["bb_percent_b"] = round(float((close - bb_lower) / (bb_upper - bb_lower)), 2)
    except KeyError:
        pass

    return detail
