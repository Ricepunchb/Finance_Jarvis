# core/risk.py
"""리스크 관리 및 서킷브레이커 대체 게이트.

이 모듈이 내리는 판단은 전부 "의심스러우면 멈춘다" 방향으로만 편향되어야 한다.
자동 재개(re-enable)는 존재하지 않으며, kill-switch 해제는 항상 사람이 명시적으로
UI에서 조작해야 한다 (자동 복구가 아직 이상한 시장에 캐치업 주문을 쏟아내는
사고를 막기 위함).
"""
import time
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import aiosqlite

from core import db
from core.config import settings

KST = ZoneInfo("Asia/Seoul")

# 관심 종목 중 이 비율 이상이 동시에 VI/거래정지 상태면 시장전체 이상으로 간주한다.
# (KIS에는 KOSPI 지수 단위 서킷브레이커 API가 없어 이것이 유일한 대체 신호)
MARKET_WIDE_HALT_BREADTH_THRESHOLD = 0.35


class RiskManager:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    # --- kill switch (수동 + 자동 트립, 해제는 항상 수동) ---

    async def is_kill_switch_active(self) -> bool:
        return await db.get_state(self.conn, "kill_switch_active") == "1"

    async def trip_kill_switch(self, reason: str) -> None:
        await db.set_state(self.conn, "kill_switch_active", "1")
        await db.set_state(self.conn, "kill_switch_reason", reason)

    async def clear_kill_switch(self) -> None:
        """사람이 UI에서 명시적으로 호출해야 한다. 엔진 내부에서 스스로 호출하지 않는다."""
        await db.set_state(self.conn, "kill_switch_active", "0")
        await db.set_state(self.conn, "kill_switch_reason", "")

    # --- 시장전체 이상 감지 (VI 확산 휴리스틱) ---

    async def check_market_wide_halt_breadth(self, halted_count: int, watched_count: int) -> bool:
        """관심종목 중 halted_count/watched_count가 임계치를 넘으면 kill switch를 트립한다."""
        if watched_count == 0:
            return False
        breadth = halted_count / watched_count
        if breadth >= MARKET_WIDE_HALT_BREADTH_THRESHOLD:
            await self.trip_kill_switch(
                f"관심종목의 {breadth:.0%}가 동시에 VI/거래정지 상태 — 시장전체 이상 의심"
            )
            return True
        return False

    async def record_order_rejection(self) -> None:
        """짧은 시간 내 주문거부가 몰리면(여러 종목) 시장전체 이상의 독립적 신호로 취급."""
        now = time.time()
        raw = await db.get_state(self.conn, "recent_rejections")
        timestamps = [float(t) for t in raw.split(",")] if raw else []
        timestamps = [t for t in timestamps if now - t < 60] + [now]
        await db.set_state(self.conn, "recent_rejections", ",".join(str(t) for t in timestamps))
        if len(timestamps) >= 3:
            await self.trip_kill_switch("60초 내 주문거부 3회 이상 — 시장전체 이상 의심")

    # --- 체결통보 웹소켓 staleness 게이트 ---

    async def is_ws_healthy(self) -> bool:
        raw = await db.get_state(self.conn, "last_ws_message_at")
        if raw is None:
            return False
        return (time.time() - float(raw)) <= settings.WS_STALENESS_THRESHOLD_SEC

    async def mark_ws_message_received(self) -> None:
        await db.set_state(self.conn, "last_ws_message_at", str(time.time()))

    # --- 쿨다운 ---

    async def is_cooldown_active(self, symbol: str) -> bool:
        cur = await self.conn.execute(
            "SELECT created_at FROM order_intents WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
            (symbol,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        return (time.time() - row["created_at"]) < settings.ORDER_COOLDOWN_SEC

    # --- 일일 손실 한도 ---

    def _today_key(self) -> str:
        return datetime.now(tz=KST).strftime("%Y%m%d")

    async def add_daily_pnl(self, amount: float) -> None:
        today = self._today_key()
        stored_date = await db.get_state(self.conn, "daily_loss_date")
        accum = 0.0
        if stored_date == today:
            accum = float(await db.get_state(self.conn, "daily_loss_accum") or 0.0)
        accum = min(0.0, accum + amount) if amount < 0 else accum
        await db.set_state(self.conn, "daily_loss_date", today)
        await db.set_state(self.conn, "daily_loss_accum", str(accum))

    async def is_daily_loss_limit_hit(self, total_equity: float) -> bool:
        today = self._today_key()
        stored_date = await db.get_state(self.conn, "daily_loss_date")
        if stored_date != today:
            return False
        accum = float(await db.get_state(self.conn, "daily_loss_accum") or 0.0)
        return abs(accum) >= settings.MAX_DAILY_LOSS_PCT * total_equity

    # --- 주문 크기 클램프 (베토가 아니라 한도 안으로 축소) ---

    def clamp_qty_to_notional(self, qty: float, price: float) -> float:
        if price <= 0:
            return qty
        max_qty = settings.MAX_ORDER_NOTIONAL_KRW / price
        return min(qty, max_qty)

    def clamp_buy_qty_to_position_cap(
        self, qty: float, price: float, current_position_value: float, total_equity: float
    ) -> float:
        """매수 후 종목 비중이 MAX_POSITION_PCT를 넘지 않도록 수량을 축소한다."""
        if total_equity <= 0 or price <= 0:
            return 0.0
        max_position_value = settings.MAX_POSITION_PCT * total_equity
        room = max_position_value - current_position_value
        if room <= 0:
            return 0.0
        return min(qty, room / price)
