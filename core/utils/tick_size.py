# core/utils/tick_size.py
"""한국거래소(KRX) 주식/ETF 및 해외주식 호가 단위(Tick Size) 보정 모듈.

한국거래소 유가증권/코스닥 시장 업무규정 시행세칙:
[일반 주식 호가단위]
- 2,000원 미만: 1원
- 2,000원 이상 ~ 5,000원 미만: 5원
- 5,000원 이상 ~ 20,000원 미만: 10원
- 20,000원 이상 ~ 50,000원 미만: 50원
- 50,000원 이상 ~ 200,000원 미만: 100원
- 200,000원 이상 ~ 500,000원 미만: 500원
- 500,000원 이상: 1,000원

[상장지수펀드(ETF) / 상장지수증권(ETN) 호가단위]
- 2,000원 미만: 1원
- 2,000원 이상: 5원 (전 구간 5원 단일화)

[해외 주식 (미국)]
- 1달러 미만: 0.0001달러 또는 0.001달러 (소수점 4자리)
- 1달러 이상: 0.01달러 (센트 단위, 소수점 2자리)
"""
import math
from typing import Union


def is_krx_etf(symbol: str) -> bool:
    """KRX 종목코드가 ETF/ETN인지 판별 (문자 포함 종목코드 또는 알려진 ETF 패턴)."""
    if not symbol:
        return False
    sym = symbol.strip().upper()
    # 종목코드에 알파벳이 들어간 경우 (예: 0040X0, 0000X0 등 합성/액티브 ETF/ETN)
    if any(c.isalpha() for c in sym):
        return True
    return False


def get_krx_tick_size(price: float, is_etf: bool = False) -> int:
    """가격 및 ETF 여부에 따른 KRX 최소 호가 단위(원) 반환."""
    p = max(0.0, float(price))
    if is_etf:
        if p < 2000.0:
            return 1
        return 5

    # 일반 주식 호가단위
    if p < 2000.0:
        return 1
    elif p < 5000.0:
        return 5
    elif p < 20000.0:
        return 10
    elif p < 50000.0:
        return 50
    elif p < 200000.0:
        return 100
    elif p < 500000.0:
        return 500
    else:
        return 1000


def adjust_to_tick_size(
    price: float,
    market: str = "domestic",
    is_etf: bool = False,
    side: str = "sell",
    round_mode: str = "nearest",
) -> Union[int, float]:
    """주문 가격을 거래소 호가 단위의 정수배로 보정합니다.

    Args:
        price: 원래 계산된 가격
        market: "domestic" | "overseas"
        is_etf: ETF 여부
        side: "buy" | "sell"
        round_mode: "nearest" (반올림), "floor" (내림), "ceil" (올림)
    """
    if price <= 0:
        return 0

    if market == "overseas":
        # 미국 주식: 1달러 이상 센트 단위 (0.01), 1달러 미만 4자리
        if price >= 1.0:
            return round(price, 2)
        return round(price, 4)

    # 국내 주식/ETF
    tick = get_krx_tick_size(price, is_etf=is_etf)
    p = float(price)

    if round_mode == "floor":
        adjusted = math.floor(p / tick) * tick
    elif round_mode == "ceil":
        adjusted = math.ceil(p / tick) * tick
    else:  # "nearest"
        adjusted = round(p / tick) * tick

    # 0원 이하 방지 (최소 1틱 보장)
    adjusted = max(tick, int(adjusted))
    return int(adjusted)

