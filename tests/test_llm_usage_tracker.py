# tests/test_llm_usage_tracker.py
import asyncio
import pytest

from core.intel.usage_tracker import BudgetExceededError, UsageTracker, estimate_cost_usd


def test_estimate_cost_usd_models():
    # gemini-3.5-flash-lite: $0.10/1M in, $0.40/1M out
    assert estimate_cost_usd("gemini-3.5-flash-lite", 1_000_000, 1_000_000) == pytest.approx(0.50)
    # gemini-4-hydrogen: $1.25/1M in, $5.00/1M out
    assert estimate_cost_usd("gemini-4-hydrogen", 1_000_000, 1_000_000) == pytest.approx(6.25)
    # local Open-Jev models: $0
    assert estimate_cost_usd("laya", 500_000, 100_000) == 0.0
    assert estimate_cost_usd("von", 500_000, 100_000) == 0.0


def test_weekly_budget_circuit_breaker_and_summary(tmp_path):
    """주간 $1.50 한도 초과 시 paid tier는 차단되고 local decision tier는 허용되는지 검증."""
    async def _run():
        db_path = str(tmp_path / "intel.db")
        tracker = UsageTracker(db_path=db_path, weekly_cost_usd_limit=1.50)

        # 초기 상태: 허용
        st0 = await tracker.assert_budget_allowed(tier="fast")
        assert st0.allowed is True
        assert st0.weekly_cost_usd == 0.0

        # gemini-4-hydrogen 호출로 $1.25 + $0.50 = $1.75 기록 -> 주간 한도 $1.50 초과
        cost = await tracker.record(
            task_type="rebalance",
            tier="smart",
            provider="gemini",
            model="gemini-4-hydrogen",
            prompt_tokens=1_000_000,
            output_tokens=100_000,
            latency_ms=420.0,
        )
        assert cost == pytest.approx(1.75)

        # 유료 티어(fast/smart)는 차단
        with pytest.raises(BudgetExceededError):
            await tracker.assert_budget_allowed(tier="fast")
        with pytest.raises(BudgetExceededError):
            await tracker.assert_budget_allowed(tier="smart")

        # 로컬 Open-Jev(decision) 티어는 $0이므로 차단되지 않음
        st_dec = await tracker.assert_budget_allowed(tier="decision")
        assert st_dec.allowed is False  # 예산 상태 자체는 초과로 표시되나 예외 없이 통과

        summary = await tracker.get_daily_summary(days=7)
        assert summary["budget"]["weekly_limit_usd"] == 1.50
        assert len(summary["daily"]) == 1
        assert summary["daily"][0]["total_tokens"] == 1_100_000
        assert summary["by_model_task"][0]["model"] == "gemini-4-hydrogen"

    asyncio.run(_run())
