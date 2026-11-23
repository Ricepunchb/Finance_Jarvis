# core/flow_signals.py
"""고래/수급(Smart Money Flow) 시그널 산출 모듈.

순수 계산 함수(I/O 없음 — 백테스트·단위테스트 1:1 공유)와 KIS REST I/O 어댑터를 명확히 분리한다.

2계층 구조:
- Layer A (가격·거래량 수급 프록시): OHLCV만으로 RVOL(상대거래량), VWAP 괴리, CMF(Chaikin Money Flow),
  OBV 기울기, (선택) 벤치마크 대비 상대강도를 계산한다. 국내·해외 공통이며 백테스트에서 100% 재현된다.
- Layer B (국내 KIS 수급): 종목별 프로그램매매 순매수 추이, 외인/기관 추정·일별 순매수, 체결강도,
  실시간 웹소켓 분 단위 스냅샷을 결합한다. 모의투자/API 장애 시 Layer A로 안전하게 폴백한다.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

import pandas as pd

from core import kis_domestic
from core.config import settings
from core.kis_client import AsyncKISClient
from core.kis_common import KisApiError

logger = logging.getLogger(__name__)

Signal = Dict[str, Any]

MIN_FLOW_BARS = 21
DIRECTION_THRESHOLD = 0.15


def _clamp(val: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _to_signal(score: float, detail: str, components: Dict[str, float]) -> Signal:
    clamped = _clamp(score)
    direction = (
        "BUY"
        if clamped > DIRECTION_THRESHOLD
        else "SELL"
        if clamped < -DIRECTION_THRESHOLD
        else "HOLD"
    )
    return {
        "direction": direction,
        "strength": round(min(1.0, abs(clamped)), 4),
        "score": round(clamped, 4),
        "detail": detail,
        "components": {k: round(v, 4) for k, v in components.items()},
    }


# ---------------------------------------------------------------------------
# Layer A: 가격·거래량 기반 수급 프록시 (순수 함수 — 국내/해외/백테스트 공용)
# ---------------------------------------------------------------------------

def _rvol_directional_score(df: pd.DataFrame, window: int, spike_threshold: float) -> float:
    """상대거래량(RVOL)과 최근 캔들 방향의 결합 점수 [-1.0, +1.0]."""
    vol = df["volume"].astype(float)
    avg_vol = float(vol.iloc[-(window + 1):-1].mean())
    if avg_vol <= 0:
        return 0.0
    last_vol = float(vol.iloc[-1])
    rvol = last_vol / avg_vol

    close_now = float(df["close"].iloc[-1])
    open_now = float(df["open"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    body = close_now - open_now
    ret = close_now - prev_close
    candle_dir = 1.0 if (body > 0 or (body == 0 and ret > 0)) else (-1.0 if (body < 0 or ret < 0) else 0.0)

    if candle_dir == 0.0 or rvol < 1.0:
        return 0.0
    # rvol=1.0 -> 0.0, rvol=spike_threshold -> ~0.75, rvol>=spike_threshold*1.5 -> 1.0
    denom = max(0.5, spike_threshold - 1.0)
    magnitude = _clamp((rvol - 1.0) / denom * 0.75, 0.0, 1.0)
    return candle_dir * magnitude


def _vwap_deviation_score(df: pd.DataFrame, window: int) -> float:
    """롤링 VWAP 대비 종가 위치 점수 [-1.0, +1.0]."""
    tail = df.iloc[-window:]
    tp = (tail["high"].astype(float) + tail["low"].astype(float) + tail["close"].astype(float)) / 3.0
    vol = tail["volume"].astype(float)
    vol_sum = float(vol.sum())
    if vol_sum <= 0:
        return 0.0
    vwap = float((tp * vol).sum()) / vol_sum
    if vwap <= 0:
        return 0.0
    dev = (float(tail["close"].iloc[-1]) - vwap) / vwap
    # ±3% 괴리를 ±1.0으로 포화
    return _clamp(dev / 0.03)


def _cmf_score(df: pd.DataFrame, window: int) -> float:
    """Chaikin Money Flow(20봉) 기반 매집(+)/분산(-) 점수 [-1.0, +1.0]."""
    tail = df.iloc[-window:]
    high = tail["high"].astype(float)
    low = tail["low"].astype(float)
    close = tail["close"].astype(float)
    vol = tail["volume"].astype(float)

    hl_range = (high - low).replace(0.0, float("nan"))
    mfm = (((close - low) - (high - close)) / hl_range).fillna(0.0)
    mfv = mfm * vol
    vol_sum = float(vol.sum())
    if vol_sum <= 0:
        return 0.0
    cmf = float(mfv.sum()) / vol_sum
    # 통상 CMF ±0.25 이상이면 강한 매집/분산
    return _clamp(cmf / 0.25)


def _obv_slope_score(df: pd.DataFrame, lookback: int = 5) -> float:
    """최근 N봉 OBV 변화 방향을 해당 구간 거래량 합 대비 비율로 정규화 [-1.0, +1.0]."""
    if len(df) < lookback + 1:
        return 0.0
    tail = df.iloc[-(lookback + 1):]
    diff = tail["close"].astype(float).diff().iloc[1:]
    vol = tail["volume"].astype(float).iloc[1:]
    vol_sum = float(vol.sum())
    if vol_sum <= 0:
        return 0.0
    signed_vol = float((vol * diff.apply(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0))).sum())
    return _clamp(signed_vol / vol_sum)


def _relative_strength_score(df: pd.DataFrame, benchmark_df: pd.DataFrame, lookback: int = 5) -> float:
    """벤치마크 대비 최근 N봉 초과수익률 점수 [-1.0, +1.0]."""
    if len(df) <= lookback or len(benchmark_df) <= lookback:
        return 0.0
    c0, c1 = float(df["close"].iloc[-(lookback + 1)]), float(df["close"].iloc[-1])
    b0, b1 = float(benchmark_df["close"].iloc[-(lookback + 1)]), float(benchmark_df["close"].iloc[-1])
    if c0 <= 0 or b0 <= 0:
        return 0.0
    excess = (c1 / c0 - 1.0) - (b1 / b0 - 1.0)
    # ±4% 초과수익을 ±1.0으로 포화
    return _clamp(excess / 0.04)


def compute_ohlcv_flow_signal(
    df: Optional[pd.DataFrame],
    benchmark_df: Optional[pd.DataFrame] = None,
    rvol_window: Optional[int] = None,
    rvol_spike_threshold: Optional[float] = None,
    cmf_window: Optional[int] = None,
) -> Signal:
    """OHLCV 기반 수급 프록시(Layer A) 시그널을 계산한다.

    데이터가 부족하면 중립 HOLD(strength=0.0)을 반환한다.
    """
    r_win = rvol_window or settings.FLOW_RVOL_WINDOW
    r_spike = rvol_spike_threshold or settings.FLOW_RVOL_SPIKE_THRESHOLD
    c_win = cmf_window or settings.FLOW_CMF_WINDOW
    min_bars = max(MIN_FLOW_BARS, r_win + 1, c_win)

    if df is None or len(df) < min_bars:
        return _to_signal(0.0, "flow_ohlcv_insufficient_data", {})

    rvol_s = _rvol_directional_score(df, r_win, r_spike)
    vwap_s = _vwap_deviation_score(df, r_win)
    cmf_s = _cmf_score(df, c_win)
    obv_s = _obv_slope_score(df, lookback=5)

    components: Dict[str, float] = {
        "rvol": rvol_s,
        "vwap": vwap_s,
        "cmf": cmf_s,
        "obv": obv_s,
    }
    weights = {"rvol": 0.30, "vwap": 0.25, "cmf": 0.25, "obv": 0.20}

    if benchmark_df is not None and len(benchmark_df) >= 6:
        rs_s = _relative_strength_score(df, benchmark_df, lookback=5)
        components["rel_strength"] = rs_s
        weights = {"rvol": 0.25, "vwap": 0.20, "cmf": 0.20, "obv": 0.15, "rel_strength": 0.20}

    score = sum(components[k] * w for k, w in weights.items())
    return _to_signal(score, f"ohlcv_flow={score:+.2f}", components)


# ---------------------------------------------------------------------------
# Layer B: 국내 KIS 수급 (프로그램 + 외인/기관 + 체결강도 + WS 스냅샷)
# ---------------------------------------------------------------------------

def compute_kis_flow_signal(
    *,
    program_rows: Optional[List[Dict[str, Any]]] = None,
    investor_rows: Optional[List[Dict[str, Any]]] = None,
    cttr: Optional[float] = None,
    ws_snapshot: Optional[Dict[str, Any]] = None,
    cttr_bull: Optional[float] = None,
    cttr_bear: Optional[float] = None,
) -> Optional[Signal]:
    """KIS 국내 수급 데이터(Layer B)를 순수 계산으로 정규화한다.

    유효한 입력이 하나도 없으면 None을 반환해 호출 측이 Layer A만으로 동작하게 한다.
    """
    bull_th = cttr_bull if cttr_bull is not None else settings.WS_MINUTE_CTTR_BULL
    bear_th = cttr_bear if cttr_bear is not None else settings.WS_MINUTE_CTTR_BEAR

    parts: List[tuple[str, float, float]] = []  # (name, score, weight)

    # 1) 종목별 프로그램매매 순매수 추이
    if program_rows:
        latest = program_rows[0]
        # KIS 필드명: whol_smtn_ntby_qty(전체합계순매수수량) 또는 ntby_qty / whol_smtn_ntby_tr_pbmn
        net_qty = _safe_float(
            latest.get("whol_smtn_ntby_qty")
            or latest.get("prdy_vrss_vol")
            or latest.get("ntby_qty")
        )
        total_vol = abs(_safe_float(latest.get("whol_smtn_seln_vol"))) + abs(
            _safe_float(latest.get("whol_smtn_shnu_vol"))
        )
        if total_vol > 0:
            prog_s = _clamp(net_qty / total_vol * 2.0)
        elif net_qty != 0:
            prog_s = 0.5 if net_qty > 0 else -0.5
        else:
            prog_s = 0.0
        parts.append(("program", prog_s, 0.40))

    # 2) 외인 + 기관 순매수 동향 (최근 최대 3영업일 가중)
    if investor_rows:
        weighted_sum = 0.0
        denom_sum = 0.0
        for idx, row in enumerate(investor_rows[:3]):
            decay = 1.0 / (idx + 1)
            frgn = _safe_float(row.get("frgn_ntby_qty"))
            orgn = _safe_float(row.get("orgn_ntby_qty"))
            prsn = _safe_float(row.get("prsn_ntby_qty"))
            whale_net = frgn + orgn
            scale = abs(frgn) + abs(orgn) + abs(prsn)
            if scale > 0:
                day_score = _clamp(whale_net / scale * 2.0)
            elif whale_net != 0:
                day_score = 0.5 if whale_net > 0 else -0.5
            else:
                day_score = 0.0
            weighted_sum += day_score * decay
            denom_sum += decay
        if denom_sum > 0:
            parts.append(("investor", _clamp(weighted_sum / denom_sum), 0.35))

    # 3) 체결강도 (REST 또는 WS 최신값)
    eff_cttr = cttr
    if eff_cttr is None and ws_snapshot is not None:
        eff_cttr = ws_snapshot.get("cttr")
    if eff_cttr is not None and eff_cttr > 0:
        if eff_cttr >= 100.0:
            span = max(10.0, bull_th - 100.0)
            cttr_s = _clamp((eff_cttr - 100.0) / span)
        else:
            span = max(10.0, 100.0 - bear_th)
            cttr_s = -_clamp((100.0 - eff_cttr) / span)
        parts.append(("cttr", cttr_s, 0.25))

    # 4) 실시간 웹소켓 1분 롤링 스냅샷 (순매수 체결비율/프로그램 델타)
    if ws_snapshot:
        imbalance = ws_snapshot.get("volume_imbalance")
        if imbalance is not None:
            parts.append(("ws_imbalance", _clamp(float(imbalance)), 0.25))

    if not parts:
        return None

    total_w = sum(w for _, _, w in parts)
    score = sum(s * w for _, s, w in parts) / total_w
    components = {name: s for name, s, _ in parts}
    return _to_signal(score, f"kis_flow={score:+.2f}", components)


def combine_flow_layers(
    ohlcv_flow: Signal,
    kis_flow: Optional[Signal] = None,
    kis_weight: float = 0.60,
) -> Signal:
    """Layer A(OHLCV 프록시)와 Layer B(국내 KIS 수급)를 결합한다."""
    if kis_flow is None:
        return ohlcv_flow
    ohlcv_score = float(ohlcv_flow.get("score", 0.0))
    kis_score = float(kis_flow.get("score", 0.0))
    w_kis = _clamp(kis_weight, 0.0, 1.0)
    score = ohlcv_score * (1.0 - w_kis) + kis_score * w_kis
    components = {
        **{f"ohlcv_{k}": v for k, v in (ohlcv_flow.get("components") or {}).items()},
        **{f"kis_{k}": v for k, v in (kis_flow.get("components") or {}).items()},
    }
    return _to_signal(score, f"flow={score:+.2f} (ohlcv={ohlcv_score:+.2f}, kis={kis_score:+.2f})", components)


# ---------------------------------------------------------------------------
# I/O 어댑터 (실전/모의 엔진에서 호출)
# ---------------------------------------------------------------------------

async def get_domestic_flow_signal(
    client: Optional[AsyncKISClient],
    symbol: str,
    df: Optional[pd.DataFrame],
    *,
    benchmark_df: Optional[pd.DataFrame] = None,
    ws_snapshot: Optional[Dict[str, Any]] = None,
) -> Signal:
    """국내 종목의 통합 수급 시그널(Layer A + Layer B)을 반환한다.

    KIS 수급 API가 모의투자 미지원·네트워크 오류 등으로 실패해도 예외를 전파하지 않고
    Layer A(OHLCV 프록시) + WS 스냅샷으로 안전하게 폴백한다.
    """
    ohlcv_sig = compute_ohlcv_flow_signal(df, benchmark_df=benchmark_df)

    program_rows: Optional[List[Dict[str, Any]]] = None
    investor_rows: Optional[List[Dict[str, Any]]] = None
    cttr: Optional[float] = None

    if client is not None and settings.ENABLE_KIS_FLOW_API:
        try:
            program_rows = await kis_domestic.get_program_trade_by_stock(client, symbol)
        except (KisApiError, Exception) as e:
            logger.debug(f"'{symbol}' 프로그램매매 추이 조회 폴백: {e}")

        try:
            investor_rows = await kis_domestic.get_investor_trend(client, symbol)
        except (KisApiError, Exception) as e:
            logger.debug(f"'{symbol}' 투자자 매매동향 조회 폴백: {e}")

        if ws_snapshot is None or ws_snapshot.get("cttr") is None:
            try:
                ccnl_rows = await kis_domestic.get_recent_executions(client, symbol)
                if ccnl_rows:
                    parsed = _safe_float(ccnl_rows[0].get("tday_rltv"), default=-1.0)
                    if parsed > 0:
                        cttr = parsed
            except (KisApiError, Exception) as e:
                logger.debug(f"'{symbol}' 체결강도 조회 폴백: {e}")

    kis_sig = compute_kis_flow_signal(
        program_rows=program_rows,
        investor_rows=investor_rows,
        cttr=cttr,
        ws_snapshot=ws_snapshot,
    )
    return combine_flow_layers(ohlcv_sig, kis_sig)


def get_overseas_flow_signal(
    df: Optional[pd.DataFrame],
    benchmark_df: Optional[pd.DataFrame] = None,
) -> Signal:
    """해외 종목의 수급 시그널(Layer A: RVOL + VWAP + CMF + OBV + 벤치마크 상대강도)."""
    return compute_ohlcv_flow_signal(df, benchmark_df=benchmark_df)
