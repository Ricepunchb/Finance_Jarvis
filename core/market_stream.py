# core/market_stream.py
"""실시간 웹소켓 틱/프로그램 스트림의 1분 롤링 집계 및 급등락·고래 스파이크 탐지기.

순수 메모리 상태 머신(I/O 없음)으로 설계해 단위 테스트로 모든 분 단위 시나리오를
결정적으로 검증할 수 있다.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Literal, Optional

from core.config import settings

EventKind = Literal["SURGE", "PLUNGE"]


@dataclass
class MinuteBucket:
    """1분 단위 체결·수급 집계 버킷."""
    minute_epoch: int          # int(ts // 60) * 60
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    vwap_num: float = 0.0
    vwap_den: float = 0.0
    last_cttr: Optional[float] = None
    program_net_vol: float = 0.0
    program_net_amt: float = 0.0
    tick_count: int = 0

    @property
    def vwap(self) -> float:
        return self.vwap_num / self.vwap_den if self.vwap_den > 0 else self.close

    @property
    def volume_imbalance(self) -> float:
        """매수 체결량 vs 매도 체결량 불균형 [-1.0, +1.0]."""
        total = self.buy_vol + self.sell_vol
        if total <= 0:
            return 0.0
        return (self.buy_vol - self.sell_vol) / total


@dataclass(frozen=True)
class StreamEvent:
    """분 단위 급등/급락 또는 고래 스파이크 감지 이벤트."""
    kind: EventKind
    symbol: str
    price: float
    ts: float
    return_pct: float
    rvol: float
    cttr: Optional[float]
    program_net_vol: float
    detail: str


class MinuteFlowAggregator:
    """종목별 1분 롤링 버킷을 유지하고 분 단위 급등/급락 이벤트를 탐지한다."""

    def __init__(
        self,
        *,
        max_minutes: int = 30,
        surge_pct: Optional[float] = None,
        plunge_pct: Optional[float] = None,
        rvol_mult: Optional[float] = None,
        cttr_bull: Optional[float] = None,
        cttr_bear: Optional[float] = None,
        debounce_sec: Optional[int] = None,
        return_lookback_minutes: int = 3,
    ):
        self.max_minutes = max(5, max_minutes)
        self.surge_pct = surge_pct if surge_pct is not None else settings.WS_MINUTE_SURGE_PCT
        self.plunge_pct = plunge_pct if plunge_pct is not None else settings.WS_MINUTE_PLUNGE_PCT
        self.rvol_mult = rvol_mult if rvol_mult is not None else settings.WS_MINUTE_RVOL_MULT
        self.cttr_bull = cttr_bull if cttr_bull is not None else settings.WS_MINUTE_CTTR_BULL
        self.cttr_bear = cttr_bear if cttr_bear is not None else settings.WS_MINUTE_CTTR_BEAR
        self.debounce_sec = debounce_sec if debounce_sec is not None else settings.WS_FASTPATH_DEBOUNCE_SEC
        self.return_lookback_minutes = max(1, return_lookback_minutes)

        self._buckets: Dict[str, Deque[MinuteBucket]] = {}
        self._last_program_cum: Dict[str, tuple[float, float]] = {}  # symbol -> (cum_vol, cum_amt)
        self._last_event_ts: Dict[tuple[str, EventKind], float] = {}

    def _get_or_create_bucket(self, symbol: str, price: float, ts: float) -> MinuteBucket:
        minute_epoch = int(ts // 60) * 60
        dq = self._buckets.setdefault(symbol, deque(maxlen=self.max_minutes))
        if dq and dq[-1].minute_epoch == minute_epoch:
            return dq[-1]
        bucket = MinuteBucket(
            minute_epoch=minute_epoch,
            open=price,
            high=price,
            low=price,
            close=price,
        )
        dq.append(bucket)
        return bucket

    def on_tick(
        self,
        symbol: str,
        price: float,
        volume: float,
        *,
        cttr: Optional[float] = None,
        side: Optional[str] = None,  # "buy" | "sell" | None
        ts: Optional[float] = None,
    ) -> Optional[StreamEvent]:
        """실시간 체결 틱을 반영하고 이벤트 조건 충족 시 StreamEvent를 반환한다."""
        if price <= 0 or volume < 0:
            return None
        now = ts if ts is not None else time.time()
        b = self._get_or_create_bucket(symbol, price, now)
        b.high = max(b.high, price)
        b.low = min(b.low, price)
        b.close = price
        b.volume += volume
        b.vwap_num += price * volume
        b.vwap_den += volume
        b.tick_count += 1
        if side == "buy":
            b.buy_vol += volume
        elif side == "sell":
            b.sell_vol += volume
        if cttr is not None and cttr > 0:
            b.last_cttr = cttr
        return self.evaluate_event(symbol, now)

    def on_program(
        self,
        symbol: str,
        cum_net_vol: float,
        cum_net_amt: float = 0.0,
        *,
        ts: Optional[float] = None,
    ) -> None:
        """실시간 프로그램 누적 순매수 데이터를 1분 델타로 반영한다."""
        now = ts if ts is not None else time.time()
        prev_vol, prev_amt = self._last_program_cum.get(symbol, (cum_net_vol, cum_net_amt))
        delta_vol = cum_net_vol - prev_vol
        delta_amt = cum_net_amt - prev_amt
        self._last_program_cum[symbol] = (cum_net_vol, cum_net_amt)

        dq = self._buckets.get(symbol)
        if not dq:
            return
        b = dq[-1]
        minute_epoch = int(now // 60) * 60
        if b.minute_epoch == minute_epoch:
            b.program_net_vol += delta_vol
            b.program_net_amt += delta_amt

    def _compute_rolling_metrics(self, symbol: str) -> Optional[Dict[str, Any]]:
        dq = self._buckets.get(symbol)
        if not dq:
            return None
        cur = dq[-1]
        # 직전 N분 기준 시가(또는 종가) 대비 수익률
        ref_idx = max(0, len(dq) - self.return_lookback_minutes)
        ref_price = dq[ref_idx].open
        if ref_price <= 0:
            return None
        ret_pct = (cur.close - ref_price) / ref_price

        # 직전 분 버킷들(최대 20분) 평균 거래량 대비 현재 분 거래량 배수
        if len(dq) >= 2:
            prior = list(dq)[:-1][-20:]
            avg_vol = sum(x.volume for x in prior) / len(prior)
            rvol = (cur.volume / avg_vol) if avg_vol > 0 else 1.0
        else:
            rvol = 1.0

        recent_prog = sum(x.program_net_vol for x in list(dq)[-self.return_lookback_minutes:])
        return {
            "price": cur.close,
            "return_pct": ret_pct,
            "rvol": rvol,
            "cttr": cur.last_cttr,
            "program_net_vol": recent_prog,
            "volume_imbalance": cur.volume_imbalance,
            "vwap": cur.vwap,
        }

    def evaluate_event(self, symbol: str, now: float) -> Optional[StreamEvent]:
        """현재 롤링 상태로 급락(PLUNGE)/급등(SURGE) 이벤트를 판정한다 (디바운스 포함)."""
        m = self._compute_rolling_metrics(symbol)
        if m is None:
            return None

        ret_pct = m["return_pct"]
        rvol = m["rvol"]
        cttr = m["cttr"]
        prog = m["program_net_vol"]

        # 1. 급락/고래 이탈 (PLUNGE) — 방어 최우선
        if ret_pct <= self.plunge_pct:
            flow_confirm = (
                rvol >= self.rvol_mult
                or (cttr is not None and cttr <= self.cttr_bear)
                or prog < 0
            )
            if flow_confirm and self._can_emit(symbol, "PLUNGE", now):
                self._last_event_ts[(symbol, "PLUNGE")] = now
                return StreamEvent(
                    kind="PLUNGE",
                    symbol=symbol,
                    price=m["price"],
                    ts=now,
                    return_pct=ret_pct,
                    rvol=rvol,
                    cttr=cttr,
                    program_net_vol=prog,
                    detail=f"PLUNGE ret={ret_pct:+.2%}, rvol={rvol:.1f}x, cttr={cttr}, prog={prog:+.0f}",
                )

        # 2. 급등/고래 유입 (SURGE) — 거래량 스파이크 + 수급 방향 확인 필수
        if ret_pct >= self.surge_pct and rvol >= self.rvol_mult:
            flow_confirm = (
                (cttr is not None and cttr >= self.cttr_bull)
                or prog > 0
                or m["volume_imbalance"] >= 0.25
            )
            if flow_confirm and self._can_emit(symbol, "SURGE", now):
                self._last_event_ts[(symbol, "SURGE")] = now
                return StreamEvent(
                    kind="SURGE",
                    symbol=symbol,
                    price=m["price"],
                    ts=now,
                    return_pct=ret_pct,
                    rvol=rvol,
                    cttr=cttr,
                    program_net_vol=prog,
                    detail=f"SURGE ret={ret_pct:+.2%}, rvol={rvol:.1f}x, cttr={cttr}, prog={prog:+.0f}",
                )

        return None

    def _can_emit(self, symbol: str, kind: EventKind, now: float) -> bool:
        last = self._last_event_ts.get((symbol, kind))
        if last is None:
            return True
        return (now - last) >= self.debounce_sec

    def get_snapshot(self, symbol: str, recent_minutes: int = 15) -> Optional[Dict[str, Any]]:
        """30분 사이클의 flow_signals(Layer B)가 참조할 실시간 수급 스냅샷을 반환한다."""
        dq = self._buckets.get(symbol)
        if not dq:
            return None
        recent = list(dq)[-recent_minutes:]
        buy_sum = sum(b.buy_vol for b in recent)
        sell_sum = sum(b.sell_vol for b in recent)
        tot = buy_sum + sell_sum
        imbalance = ((buy_sum - sell_sum) / tot) if tot > 0 else 0.0
        cttr = next((b.last_cttr for b in reversed(recent) if b.last_cttr is not None), None)
        return {
            "last_price": recent[-1].close,
            "vwap": recent[-1].vwap,
            "cttr": cttr,
            "volume_imbalance": round(imbalance, 4),
            "program_net_vol": sum(b.program_net_vol for b in recent),
            "minutes_collected": len(recent),
        }
