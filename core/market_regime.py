# core/market_regime.py
"""시장 전체 레짐(RISK_ON / NEUTRAL / RISK_OFF) 판정 모듈.

개별 종목 지표가 양호하더라도 코스피 매도 사이드카 같은 시장 전체 폭락일에는
신규 매수·온보딩이 직후 손절로 이어지기 쉽다.
순수 판정 함수(evaluate_market_regime)는 실전과 백테스트에서 동일하게 사용된다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

import pandas as pd

from core import indicators, kis_domestic
from core.config import settings
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)

RegimeLabel = Literal["RISK_ON", "NEUTRAL", "RISK_OFF"]


@dataclass(frozen=True)
class MarketRegime:
    regime: RegimeLabel
    benchmark: str
    intraday_change_pct: float
    below_sma20: bool
    market_program_net: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime,
            "benchmark": self.benchmark,
            "intraday_change_pct": round(self.intraday_change_pct, 5),
            "below_sma20": self.below_sma20,
            "market_program_net": self.market_program_net,
            "reason": self.reason,
        }


def is_defensive_asset(symbol: str, name: str = "", keywords_csv: Optional[str] = None) -> bool:
    """채권·금·단기국채 등 RISK_OFF 레짐에서도 매수를 허용할 방어 자산인지 판별한다."""
    raw = keywords_csv if keywords_csv is not None else settings.MARKET_REGIME_DEFENSIVE_KEYWORDS
    keywords = [k.strip().upper() for k in raw.split(",") if k.strip()]
    if not keywords:
        return False
    haystack = f"{symbol} {name}".upper()
    return any(kw in haystack for kw in keywords)


def evaluate_market_regime(
    benchmark_df: Optional[pd.DataFrame],
    *,
    benchmark_symbol: str = "",
    intraday_change_pct: Optional[float] = None,
    market_program_net: Optional[float] = None,
    intraday_drop_threshold: Optional[float] = None,
) -> MarketRegime:
    """벤치마크 일봉 추세 + 당일 등락률 + (선택) 시장 프로그램 순매수로 시장 레짐을 판정한다.

    순수 함수 — 백테스트에서는 benchmark_df의 당일 종가 수익률로 intraday_change_pct를 자동 계산한다.
    """
    drop_th = (
        intraday_drop_threshold
        if intraday_drop_threshold is not None
        else settings.MARKET_REGIME_INTRADAY_DROP_PCT
    )

    change_pct = intraday_change_pct
    below_sma20 = False
    above_sma20_and_60 = False

    if benchmark_df is not None and len(benchmark_df) >= 2:
        closes = benchmark_df["close"].astype(float)
        if change_pct is None and closes.iloc[-2] > 0:
            change_pct = float(closes.iloc[-1] / closes.iloc[-2] - 1.0)
        if len(closes) >= 20:
            sma20 = float(closes.iloc[-20:].mean())
            below_sma20 = float(closes.iloc[-1]) < sma20
            if len(closes) >= 60:
                sma60 = float(closes.iloc[-60:].mean())
                above_sma20_and_60 = (float(closes.iloc[-1]) >= sma20) and (sma20 >= sma60)
            else:
                above_sma20_and_60 = not below_sma20

    eff_change = float(change_pct) if change_pct is not None else 0.0

    # 1. 급락일 (사이드카/블랙먼데이 등): 즉시 RISK_OFF
    if eff_change <= drop_th:
        return MarketRegime(
            regime="RISK_OFF",
            benchmark=benchmark_symbol,
            intraday_change_pct=eff_change,
            below_sma20=below_sma20,
            market_program_net=market_program_net,
            reason=f"벤치마크({benchmark_symbol}) 당일 급락 {eff_change:+.2%} <= {drop_th:+.2%}",
        )

    # 2. 20일선 하회 + 시장 프로그램 순매도 동반 (또는 20일선 하회 + 당일 -1% 이상 하락)
    if below_sma20 and (
        (market_program_net is not None and market_program_net < 0)
        or eff_change <= drop_th * 0.5
    ):
        return MarketRegime(
            regime="RISK_OFF",
            benchmark=benchmark_symbol,
            intraday_change_pct=eff_change,
            below_sma20=True,
            market_program_net=market_program_net,
            reason=f"벤치마크({benchmark_symbol}) SMA20 하회 + 하락/프로그램 매도({eff_change:+.2%})",
        )

    if above_sma20_and_60 and eff_change >= 0.0:
        return MarketRegime(
            regime="RISK_ON",
            benchmark=benchmark_symbol,
            intraday_change_pct=eff_change,
            below_sma20=False,
            market_program_net=market_program_net,
            reason=f"벤치마크({benchmark_symbol}) 상승 추세 ({eff_change:+.2%})",
        )

    return MarketRegime(
        regime="NEUTRAL",
        benchmark=benchmark_symbol,
        intraday_change_pct=eff_change,
        below_sma20=below_sma20,
        market_program_net=market_program_net,
        reason=f"중립 ({eff_change:+.2%})",
    )


class MarketRegimeCache:
    """사이클 내 벤치마크/시장 프로그램 조회를 TTL 동안 캐싱한다."""

    def __init__(self, ttl_sec: Optional[int] = None):
        self.ttl_sec = ttl_sec if ttl_sec is not None else settings.MARKET_REGIME_CACHE_TTL_SEC
        self._cache: Dict[str, tuple[float, MarketRegime]] = {}

    def get(self, key: str, now: Optional[float] = None) -> Optional[MarketRegime]:
        ts_now = now if now is not None else time.time()
        entry = self._cache.get(key)
        if entry is None:
            return None
        cached_at, regime = entry
        if (ts_now - cached_at) > self.ttl_sec:
            return None
        return regime

    def set(self, key: str, regime: MarketRegime, now: Optional[float] = None) -> None:
        self._cache[key] = (now if now is not None else time.time(), regime)


async def fetch_domestic_market_regime(
    client: AsyncKISClient,
    benchmark_df: Optional[pd.DataFrame],
    cache: Optional[MarketRegimeCache] = None,
) -> MarketRegime:
    """국내 시장 레짐을 조회한다 (KODEX 200 당일 등락률 + 코스피 프로그램 순매수)."""
    if not settings.MARKET_REGIME_ENABLED:
        return MarketRegime("NEUTRAL", settings.MARKET_REGIME_DOMESTIC_BENCHMARK, 0.0, False, None, "disabled")

    if cache is not None:
        cached = cache.get("domestic")
        if cached is not None:
            return cached

    bench = settings.MARKET_REGIME_DOMESTIC_BENCHMARK
    intraday_change: Optional[float] = None
    market_prog_net: Optional[float] = None

    try:
        q = await kis_domestic.get_price(client, bench)
        rate = q.get("prdy_ctrt")
        if rate not in (None, ""):
            intraday_change = float(rate) / 100.0
    except Exception as e:
        logger.debug(f"시장 레짐 벤치마크({bench}) 현재가 조회 실패 - 일봉 폴백: {e}")

    if settings.ENABLE_KIS_FLOW_API:
        try:
            rows = await kis_domestic.get_market_program_today(client, mrkt_cls_code="1")
            if rows:
                # KIS 필드: whol_smtn_ntby_tr_pbmn(전체합계순매수거래대금) 또는 ntby_tr_pbmn
                raw_net = rows[0].get("whol_smtn_ntby_tr_pbmn") or rows[0].get("ntby_tr_pbmn")
                if raw_net not in (None, ""):
                    market_prog_net = float(raw_net)
        except Exception as e:
            logger.debug(f"시장 전체 프로그램 매매동향 조회 폴백: {e}")

    regime = evaluate_market_regime(
        benchmark_df,
        benchmark_symbol=bench,
        intraday_change_pct=intraday_change,
        market_program_net=market_prog_net,
    )
    if cache is not None:
        cache.set("domestic", regime)
    return regime
