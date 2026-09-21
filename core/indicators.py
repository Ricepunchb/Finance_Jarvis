# core/indicators.py
"""기술적 지표 기반 매매 시그널. pandas-ta로 계산하며 수식을 직접 구현하지 않는다."""
from typing import Any, Dict, List

import pandas as pd
import pandas_ta as ta

MIN_BARS_REQUIRED = 30


def _first_matching_column(df: pd.DataFrame, prefix: str) -> str:
    for col in df.columns:
        if col.startswith(prefix):
            return col
    raise KeyError(f"'{prefix}'로 시작하는 컬럼을 찾을 수 없습니다: {list(df.columns)}")


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

    votes = []

    rsi = last[_first_matching_column(work, "RSI_")]
    if pd.notna(rsi):
        if rsi < 30:
            votes.append(1.0)
        elif rsi > 70:
            votes.append(-1.0)
        else:
            votes.append(0.0)

    macd = last[_first_matching_column(work, "MACD_")]
    macd_signal = last[_first_matching_column(work, "MACDs_")]
    if pd.notna(macd) and pd.notna(macd_signal):
        votes.append(1.0 if macd > macd_signal else -1.0)

    bb_lower = last[_first_matching_column(work, "BBL_")]
    bb_upper = last[_first_matching_column(work, "BBU_")]
    close = last["close"]
    if pd.notna(bb_lower) and pd.notna(bb_upper):
        if close <= bb_lower:
            votes.append(1.0)
        elif close >= bb_upper:
            votes.append(-1.0)
        else:
            votes.append(0.0)

    score = sum(votes) / len(votes) if votes else 0.0
    direction = "BUY" if score > 0.15 else "SELL" if score < -0.15 else "HOLD"
    return {"direction": direction, "strength": min(1.0, abs(score)), "detail": f"score={score:.2f}"}
