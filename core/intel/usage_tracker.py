# core/intel/usage_tracker.py
"""일일·주간 LLM 토큰 사용량 추적 원장 및 예산 서킷브레이커.

모든 LLM/System-1 호출의 토큰 수(입력/출력/사고)와 추정 비용(USD)을
`data/jarvis_intel.db`의 `llm_usage_ledger` 테이블에 기록하고,
주간 예산(기본 $1.50 ≈ ₩2,000) 또는 일일 상한 초과 시 유료 LLM 호출을
자동 차단(BudgetExceededError)하여 안전한 L0/로컬 폴백으로 강하시킨다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import aiosqlite

from core.config import settings
from core.intel.db import get_intel_connection, init_intel_db

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 모델별 1M 토큰당 추정 단가 (USD): (input_per_1m, output_per_1m)
# 공식 단가 변동 시 이 매핑만 갱신하면 된다. 로컬 Open-Jev 모델은 $0.0.
MODEL_PRICING_USD_PER_1M: Dict[str, Tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.6-flash": (0.30, 1.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-4-hydrogen": (1.25, 5.00),
    "gemini-2.5-flash": (0.15, 0.60),
    "open-jev-local": (0.0, 0.0),
    "laya": (0.0, 0.0),
    "clef": (0.0, 0.0),
    "von": (0.0, 0.0),
}
_DEFAULT_PRICING = (0.75, 3.75)


class BudgetExceededError(RuntimeError):
    """주간 또는 일일 LLM 예산 상한 초과 시 발생하는 서킷브레이커 예외."""


@dataclass(frozen=True)
class BudgetStatus:
    allowed: bool
    date_kst: str
    week_kst: str
    daily_tokens: int
    daily_cost_usd: float
    weekly_tokens: int
    weekly_cost_usd: float
    weekly_limit_usd: float
    daily_limit_usd: Optional[float]
    daily_token_limit: Optional[int]
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "date_kst": self.date_kst,
            "week_kst": self.week_kst,
            "daily_tokens": self.daily_tokens,
            "daily_cost_usd": round(self.daily_cost_usd, 6),
            "weekly_tokens": self.weekly_tokens,
            "weekly_cost_usd": round(self.weekly_cost_usd, 6),
            "weekly_limit_usd": self.weekly_limit_usd,
            "weekly_utilization_pct": round(
                (self.weekly_cost_usd / self.weekly_limit_usd * 100.0) if self.weekly_limit_usd > 0 else 0.0,
                2,
            ),
            "daily_limit_usd": self.daily_limit_usd,
            "daily_token_limit": self.daily_token_limit,
            "reason": self.reason,
        }


def kst_date_and_week(ts: Optional[float] = None) -> Tuple[str, str]:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=KST)
    iso = dt.isocalendar()
    return dt.strftime("%Y-%m-%d"), f"{iso.year}-W{iso.week:02d}"


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    output_tokens: int,
    thought_tokens: int = 0,
) -> float:
    """모델 단가 테이블 기준으로 1회 호출의 추정 비용(USD)을 계산한다."""
    key = (model or "").strip().lower()
    if key.startswith("open-jev") or key in ("laya", "clef", "von"):
        return 0.0
    in_price, out_price = MODEL_PRICING_USD_PER_1M.get(key, _DEFAULT_PRICING)
    billable_out = max(0, output_tokens) + max(0, thought_tokens)
    return (max(0, prompt_tokens) * in_price + billable_out * out_price) / 1_000_000.0


class UsageTracker:
    """공유 인텔리전스 DB 기반 일일·주간 LLM 사용량 추적 및 예산 가드."""

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        weekly_cost_usd_limit: Optional[float] = None,
        daily_cost_usd_limit: Optional[float] = None,
        daily_token_limit: Optional[int] = None,
    ) -> None:
        self.db_path = db_path
        self.weekly_cost_usd_limit = (
            weekly_cost_usd_limit
            if weekly_cost_usd_limit is not None
            else float(getattr(settings, "LLM_WEEKLY_COST_USD_LIMIT", 1.50))
        )
        self.daily_cost_usd_limit = (
            daily_cost_usd_limit
            if daily_cost_usd_limit is not None
            else getattr(settings, "LLM_DAILY_COST_USD_LIMIT", None)
        )
        self.daily_token_limit = (
            daily_token_limit
            if daily_token_limit is not None
            else getattr(settings, "LLM_DAILY_TOKEN_LIMIT", None)
        )
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await init_intel_db(self.db_path)
            self._initialized = True

    async def record(
        self,
        *,
        task_type: str,
        tier: str,
        provider: str,
        model: str,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
        thought_tokens: int = 0,
        total_tokens: Optional[int] = None,
        latency_ms: float = 0.0,
        status: str = "ok",
        profile: Optional[str] = None,
        conn: Optional[aiosqlite.Connection] = None,
        ts: Optional[float] = None,
    ) -> float:
        now = ts if ts is not None else time.time()
        date_kst, week_kst = kst_date_and_week(now)
        total = total_tokens if total_tokens is not None else (prompt_tokens + output_tokens + thought_tokens)
        cost = estimate_cost_usd(model, prompt_tokens, output_tokens, thought_tokens)
        prof = profile or getattr(settings, "PROFILE_NAME", "default")

        close_conn = conn is None
        if conn is None:
            await self._ensure_init()
            conn = await get_intel_connection(self.db_path)
        try:
            await conn.execute(
                """
                INSERT INTO llm_usage_ledger (
                    date_kst, week_kst, timestamp, profile, task_type, tier, provider, model,
                    prompt_tokens, output_tokens, thought_tokens, total_tokens,
                    estimated_cost_usd, latency_ms, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    date_kst,
                    week_kst,
                    now,
                    prof,
                    task_type,
                    tier,
                    provider,
                    model,
                    int(prompt_tokens),
                    int(output_tokens),
                    int(thought_tokens),
                    int(total),
                    float(cost),
                    float(latency_ms),
                    status,
                ),
            )
            await conn.commit()
            return cost
        finally:
            if close_conn:
                await conn.close()

    async def get_budget_status(
        self,
        *,
        conn: Optional[aiosqlite.Connection] = None,
        ts: Optional[float] = None,
    ) -> BudgetStatus:
        date_kst, week_kst = kst_date_and_week(ts)
        close_conn = conn is None
        if conn is None:
            await self._ensure_init()
            conn = await get_intel_connection(self.db_path)
        try:
            cur_d = await conn.execute(
                """
                SELECT COALESCE(SUM(total_tokens), 0) AS tok,
                       COALESCE(SUM(estimated_cost_usd), 0.0) AS cost
                FROM llm_usage_ledger
                WHERE date_kst = ? AND status = 'ok'
                """,
                (date_kst,),
            )
            row_d = await cur_d.fetchone()
            daily_tokens = int(row_d["tok"]) if row_d else 0
            daily_cost = float(row_d["cost"]) if row_d else 0.0

            cur_w = await conn.execute(
                """
                SELECT COALESCE(SUM(total_tokens), 0) AS tok,
                       COALESCE(SUM(estimated_cost_usd), 0.0) AS cost
                FROM llm_usage_ledger
                WHERE week_kst = ? AND status = 'ok'
                """,
                (week_kst,),
            )
            row_w = await cur_w.fetchone()
            weekly_tokens = int(row_w["tok"]) if row_w else 0
            weekly_cost = float(row_w["cost"]) if row_w else 0.0
        finally:
            if close_conn:
                await conn.close()

        reason: Optional[str] = None
        allowed = True
        if self.weekly_cost_usd_limit > 0 and weekly_cost >= self.weekly_cost_usd_limit:
            allowed = False
            reason = (
                f"주간 LLM 예산 초과 (${weekly_cost:.4f} / ${self.weekly_cost_usd_limit:.2f}, {week_kst})"
            )
        elif (
            self.daily_cost_usd_limit is not None
            and self.daily_cost_usd_limit > 0
            and daily_cost >= self.daily_cost_usd_limit
        ):
            allowed = False
            reason = (
                f"일일 LLM 비용 한도 초과 (${daily_cost:.4f} / ${self.daily_cost_usd_limit:.2f}, {date_kst})"
            )
        elif (
            self.daily_token_limit is not None
            and self.daily_token_limit > 0
            and daily_tokens >= self.daily_token_limit
        ):
            allowed = False
            reason = (
                f"일일 LLM 토큰 한도 초과 ({daily_tokens:,} / {self.daily_token_limit:,}, {date_kst})"
            )

        return BudgetStatus(
            allowed=allowed,
            date_kst=date_kst,
            week_kst=week_kst,
            daily_tokens=daily_tokens,
            daily_cost_usd=daily_cost,
            weekly_tokens=weekly_tokens,
            weekly_cost_usd=weekly_cost,
            weekly_limit_usd=self.weekly_cost_usd_limit,
            daily_limit_usd=self.daily_cost_usd_limit,
            daily_token_limit=self.daily_token_limit,
            reason=reason,
        )

    async def assert_budget_allowed(
        self,
        *,
        tier: str = "fast",
        conn: Optional[aiosqlite.Connection] = None,
    ) -> BudgetStatus:
        """유료 티어(fast/smart) 호출 전 예산 한도를 검사하고 초과 시 예외를 발생시킨다."""
        status = await self.get_budget_status(conn=conn)
        if tier == "decision":
            return status  # 로컬 Open-Jev ($0)는 예산 차단 대상 아님
        if not status.allowed:
            logger.warning(f"LLM 예산 서킷브레이커 발동: {status.reason}")
            raise BudgetExceededError(status.reason or "LLM budget exceeded")
        return status

    async def get_daily_summary(
        self,
        *,
        days: int = 7,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> Dict[str, Any]:
        """최근 N일간의 일별·모델별·태스크별 LLM 사용량 및 현재 예산 상태를 반환한다."""
        now_dt = datetime.now(tz=KST)
        start_date = (now_dt - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")

        close_conn = conn is None
        if conn is None:
            await self._ensure_init()
            conn = await get_intel_connection(self.db_path)
        try:
            budget = await self.get_budget_status(conn=conn)

            cur_daily = await conn.execute(
                """
                SELECT date_kst,
                       COUNT(*) AS calls,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(thought_tokens) AS thought_tokens,
                       SUM(total_tokens) AS total_tokens,
                       ROUND(SUM(estimated_cost_usd), 6) AS estimated_cost_usd
                FROM llm_usage_ledger
                WHERE date_kst >= ? AND status = 'ok'
                GROUP BY date_kst
                ORDER BY date_kst DESC
                """,
                (start_date,),
            )
            daily_rows = [dict(r) for r in await cur_daily.fetchall()]

            cur_model = await conn.execute(
                """
                SELECT model, tier, task_type,
                       COUNT(*) AS calls,
                       SUM(total_tokens) AS total_tokens,
                       ROUND(SUM(estimated_cost_usd), 6) AS estimated_cost_usd,
                       ROUND(AVG(latency_ms), 1) AS avg_latency_ms
                FROM llm_usage_ledger
                WHERE date_kst >= ? AND status = 'ok'
                GROUP BY model, tier, task_type
                ORDER BY estimated_cost_usd DESC, calls DESC
                """,
                (start_date,),
            )
            by_model_task = [dict(r) for r in await cur_model.fetchall()]
        finally:
            if close_conn:
                await conn.close()

        return {
            "budget": budget.to_dict(),
            "days": days,
            "daily": daily_rows,
            "by_model_task": by_model_task,
        }
