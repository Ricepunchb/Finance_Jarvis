# tests/test_gemini_provider_routing.py
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.genai import errors as genai_errors

from core.config import settings
from core.intel.usage_tracker import UsageTracker
from core.llm.base import ModelTier
from core.llm.gemini_provider import GeminiProvider


class _DummyResponse:
    def __init__(self, parsed, prompt_tokens=100, output_tokens=50):
        self.parsed = parsed
        self.usage_metadata = SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            thoughts_token_count=0,
            total_token_count=prompt_tokens + output_tokens,
        )


def test_402_credit_exhausted_fails_immediately_without_retry(tmp_path, monkeypatch):
    """402 RESOURCE_EXHAUSTED 발생 시 429 재시도(14초 대기) 없이 1회 호출 후 즉시 폴백되는지 검증."""
    async def _run():
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(settings, "GEMINI_FAST_MODEL", "gemini-3.5-flash-lite")
        monkeypatch.setattr(settings, "GEMINI_FAST_FALLBACK_MODEL", "gemini-3.6-flash")

        tracker = UsageTracker(db_path=str(tmp_path / "intel.db"), weekly_cost_usd_limit=1.50)
        provider = GeminiProvider(usage_tracker=tracker)

        call_count = 0

        async def _fake_generate_content(**kwargs):
            nonlocal call_count
            call_count += 1
            raise genai_errors.ClientError(
                402,
                {"error": {"code": 402, "message": "Your prepayment credits are depleted.", "status": "RESOURCE_EXHAUSTED"}},
            )

        provider._client = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=_fake_generate_content))
        )

        t0 = time.perf_counter()
        res = await provider.analyze_news("NVDA", "Test title", "Test summary")
        elapsed = time.perf_counter() - t0

        assert res["direction"] == "HOLD"
        assert res["strength"] == 0.0
        assert call_count == 1  # 재시도 0회, 백업 모델 전환 0회
        assert elapsed < 1.0

    asyncio.run(_run())


def test_tier_model_routing_and_usage_recording(tmp_path, monkeypatch):
    """뉴스 분석은 gemini-3.5-flash-lite(FAST), 리밸런싱은 gemini-4-hydrogen(SMART)으로 라우팅되는지 검증."""
    async def _run():
        monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(settings, "GEMINI_FAST_MODEL", "gemini-3.5-flash-lite")
        monkeypatch.setattr(settings, "GEMINI_SMART_MODEL", "gemini-4-hydrogen")

        tracker = UsageTracker(db_path=str(tmp_path / "intel.db"), weekly_cost_usd_limit=1.50)
        provider = GeminiProvider(usage_tracker=tracker)

        called_models = []

        async def _fake_generate_content(*, model, contents, config):
            called_models.append(model)
            schema = config.response_schema
            if schema.__name__ == "_NewsSentimentSchema":
                parsed = schema(direction="BUY", strength=0.7, reasoning="호실적")
            else:
                parsed = schema(
                    adds=[],
                    removes=[],
                    weights=[{"symbol": "005930", "weight": 0.5}],
                    rationale="유지",
                )
            return _DummyResponse(parsed, prompt_tokens=200, output_tokens=80)

        provider._client = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=_fake_generate_content))
        )

        # 1. 뉴스 분석 -> FAST 티어
        news_res = await provider.analyze_news("005930", "삼성전자 실적 서프라이즈", "영업이익 급증")
        assert news_res["direction"] == "BUY"

        # 2. 포트폴리오 리밸런싱 -> SMART 티어
        reb_res = await provider.propose_portfolio_changes(
            current_positions=[{"symbol": "005930", "weight": 0.5, "qty": 10, "avg_price": 70000}],
            current_signals={},
            candidate_pool=[],
            macro_context=None,
            max_symbols=10,
            min_weight=0.03,
            max_weight=0.60,
        )
        assert reb_res["degraded"] is False

        assert called_models == ["gemini-3.5-flash-lite", "gemini-4-hydrogen"]

        summary = await tracker.get_daily_summary(days=1)
        assert summary["daily"][0]["calls"] == 2
        assert summary["daily"][0]["total_tokens"] == 560

    asyncio.run(_run())
