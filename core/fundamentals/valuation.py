# core/fundamentals/valuation.py
"""저평가/고평가 판단을 signal_engine이 먹는 Signal({"direction","strength"}) 형태로
만든다. DCF 같은 정밀 밸류에이션이 아니라 퍼센타일/괴리율 휴리스틱 — 혼자 운영하는
봇이 매 사이클 안정적으로 계산할 수 있는 수준으로 의도적으로 단순화했다.

3개 컴포넌트를 가중합산한다:
  1) 자기 과거 PER/PBR 백분위 (0.40) — 싸게 거래되는 중이면 매수 쪽
  2) 컨센서스 목표주가 괴리율 (0.35) — 네이버 컨센서스, 라이브 검증됨
  3) 52주 레인지 내 위치 (0.25) — 저점 근처면 매수 쪽
ROE/부채비율은 방향을 뒤집지 않고 BUY 강도만 감쇠시키는 품질 게이트로만 쓴다.

`recomm_mean`(증권사 컨센서스 등급)은 방향성(숫자가 클수록 매수 쪽인지)을 표본 1개로만
확인해 스코어에는 안 쓰고 context 참고용으로만 남긴다.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import aiosqlite
import asyncio
import pandas as pd

from core import db, indicators, kis_domestic
from core.config import settings
from core.fundamentals.naver_consensus import fetch_consensus
from core.kis_client import AsyncKISClient

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

_WEIGHT_PER_PBR_PERCENTILE = 0.40
_WEIGHT_TARGET_GAP = 0.35
_WEIGHT_52W_POSITION = 0.25
_TARGET_GAP_CAP_PCT = 0.30


def _per_pbr_percentile_score(financial_ratio_rows: list, daily_chart_rows: list) -> Optional[float]:
    """과거 EPS/BPS × 과거 종가로 PER/PBR 시계열을 만들어 '오늘'이 그중 어디쯤인지 계산.

    낮은 백분위(과거 대비 싸게 거래)일수록 +1에 가깝게, 높을수록 -1에 가깝게.
    """
    if not financial_ratio_rows or not daily_chart_rows:
        return None

    ratios = pd.DataFrame(financial_ratio_rows)
    ratios["eps"] = pd.to_numeric(ratios.get("eps"), errors="coerce")
    ratios["bps"] = pd.to_numeric(ratios.get("bps"), errors="coerce")
    ratios["period_date"] = pd.to_datetime(ratios["stac_yymm"], format="%Y%m")
    ratios = ratios.dropna(subset=["period_date"]).sort_values("period_date")
    if ratios.empty:
        return None

    prices = pd.DataFrame(daily_chart_rows)
    if "stck_clpr" not in prices.columns or "stck_bsop_date" not in prices.columns:
        return None
    prices["close"] = pd.to_numeric(prices["stck_clpr"], errors="coerce")
    prices["date"] = pd.to_datetime(prices["stck_bsop_date"], format="%Y%m%d")
    prices = prices.dropna(subset=["close", "date"]).sort_values("date")
    if prices.empty:
        return None

    merged = pd.merge_asof(prices, ratios, left_on="date", right_on="period_date", direction="backward")
    merged = merged.dropna(subset=["eps"])

    per_series = (merged["close"] / merged["eps"]).replace([float("inf"), float("-inf")], pd.NA).dropna()
    pbr_series = (merged["close"] / merged["bps"]).replace([float("inf"), float("-inf")], pd.NA).dropna() \
        if "bps" in merged.columns else pd.Series(dtype=float)

    today_eps = ratios.iloc[-1]["eps"]
    today_bps = ratios.iloc[-1]["bps"]
    today_close = prices.iloc[-1]["close"]

    if today_eps and today_eps > 0 and len(per_series) >= 10:
        today_per = today_close / today_eps
        percentile = (per_series < today_per).mean()
    elif today_bps and today_bps > 0 and len(pbr_series) >= 10:
        today_pbr = today_close / today_bps
        percentile = (pbr_series < today_pbr).mean()
    else:
        return None

    return 1.0 - 2.0 * percentile


def _target_gap_score(target_price_mean: Optional[float], current_price: float) -> Optional[float]:
    if not target_price_mean or current_price <= 0:
        return None
    gap = (target_price_mean - current_price) / current_price
    return max(-1.0, min(1.0, gap / _TARGET_GAP_CAP_PCT))


def _w52_position_score(w52_high: Optional[float], w52_low: Optional[float], current_price: float) -> Optional[float]:
    if not w52_high or not w52_low or w52_high <= w52_low:
        return None
    position_pct = (current_price - w52_low) / (w52_high - w52_low)
    position_pct = max(0.0, min(1.0, position_pct))
    return 1.0 - 2.0 * position_pct


def _quality_gate_multiplier(financial_ratio_rows: list) -> float:
    if not financial_ratio_rows:
        return 1.0
    try:
        latest = financial_ratio_rows[0]
        roe = float(latest.get("roe_val") or 0)
        debt_ratio = float(latest.get("lblt_rate") or 0)
    except (TypeError, ValueError):
        return 1.0
    if roe < -10 or debt_ratio > 200:
        return 0.5
    return 1.0


async def get_valuation_signal(
    conn: aiosqlite.Connection, client: AsyncKISClient, symbol: str
) -> Optional[Dict[str, Any]]:
    """국내 종목 전용. 하루에 한 번만 재계산(캐시 TTL) — 밸류에이션은 분기 단위로만
    바뀌므로 30분 사이클마다 다시 계산할 이유가 없다."""
    cached = await db.get_cached_valuation(conn, symbol, settings.VALUATION_CACHE_TTL_HOURS)
    if cached is not None:
        return _cached_row_to_signal(cached)

    try:
        consensus = await asyncio.to_thread(fetch_consensus, symbol)
        price_info = await kis_domestic.get_price(client, symbol)
        current_price = float(price_info.get("stck_prpr") or 0)
        if current_price <= 0:
            return None

        financial_ratio_rows: list = []
        per_pbr_score = None
        try:
            financial_ratio_rows = await kis_domestic.get_financial_ratio(client, symbol)
        except Exception:
            logger.warning(f"'{symbol}' 재무비율 조회 실패 - PER/PBR 백분위 컴포넌트 생략", exc_info=True)

        if financial_ratio_rows:
            end_date = datetime.now(tz=KST).strftime("%Y%m%d")
            start_date = (datetime.now(tz=KST) - timedelta(days=730)).strftime("%Y%m%d")
            daily_rows = await kis_domestic.get_daily_chart(client, symbol, start_date, end_date)
            per_pbr_score = _per_pbr_percentile_score(financial_ratio_rows, daily_rows)

        target_gap_score = _target_gap_score(consensus.get("target_price_mean"), current_price)
        w52_score = _w52_position_score(
            consensus.get("w52_high") or float(price_info.get("w52_hgpr") or 0) or None,
            consensus.get("w52_low") or float(price_info.get("w52_lwpr") or 0) or None,
            current_price,
        )

        parts = []
        if per_pbr_score is not None:
            parts.append((per_pbr_score, _WEIGHT_PER_PBR_PERCENTILE))
        if target_gap_score is not None:
            parts.append((target_gap_score, _WEIGHT_TARGET_GAP))
        if w52_score is not None:
            parts.append((w52_score, _WEIGHT_52W_POSITION))

        if not parts:
            return None

        total_weight = sum(w for _, w in parts)
        score = sum(s * w for s, w in parts) / total_weight
        if score > 0:
            score *= _quality_gate_multiplier(financial_ratio_rows)

        per = consensus.get("per")
        pbr = consensus.get("pbr")
        roe = float(financial_ratio_rows[0].get("roe_val")) if financial_ratio_rows else None
        debt_ratio = float(financial_ratio_rows[0].get("lblt_rate")) if financial_ratio_rows else None

        await db.cache_valuation(
            conn, symbol,
            per=per, pbr=pbr,
            per_percentile=per_pbr_score, pbr_percentile=None,
            target_price_mean=consensus.get("target_price_mean"),
            target_gap_pct=target_gap_score, recomm_mean=consensus.get("recomm_mean"),
            w52_position_pct=w52_score, roe=roe, debt_ratio=debt_ratio,
            raw_json=None,
        )
        return _score_to_signal(score)
    except Exception:
        logger.exception(f"'{symbol}' 밸류에이션 시그널 계산 실패 - 이번 사이클은 밸류에이션 없이 진행")
        return None


def _score_to_signal(score: float) -> Dict[str, Any]:
    direction = "BUY" if score > 0.15 else "SELL" if score < -0.15 else "HOLD"
    return {"direction": direction, "strength": min(1.0, abs(score))}


def _cached_row_to_signal(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    parts = []
    if row.get("per_percentile") is not None:
        parts.append((row["per_percentile"], _WEIGHT_PER_PBR_PERCENTILE))
    if row.get("target_gap_pct") is not None:
        parts.append((row["target_gap_pct"], _WEIGHT_TARGET_GAP))
    if row.get("w52_position_pct") is not None:
        parts.append((row["w52_position_pct"], _WEIGHT_52W_POSITION))
    if not parts:
        return None
    total_weight = sum(w for _, w in parts)
    score = sum(s * w for s, w in parts) / total_weight
    return _score_to_signal(score)
