# core/llm/factory.py
"""Provider-agnostic LLM 팩토리 (3티어 라우터 및 Open-Jev 섀도/활성 래핑 지원)."""
from __future__ import annotations

import logging
from typing import Optional

from core.config import settings
from core.llm.base import LLMProvider

logger = logging.getLogger(__name__)

_cached_provider: Optional[LLMProvider] = None


def reset_llm_provider() -> None:
    """테스트 또는 런타임 설정 변경 시 캐시된 provider를 초기화한다."""
    global _cached_provider
    _cached_provider = None


def get_llm_provider() -> LLMProvider:
    """settings.LLM_PROVIDER 및 OPEN_JEV_MODE에 따라 적절한 provider를 반환한다."""
    global _cached_provider
    if _cached_provider is not None:
        return _cached_provider

    if settings.LLM_PROVIDER == "gemini":
        from core.llm.gemini_provider import GeminiProvider

        base_provider: LLMProvider = GeminiProvider()
    else:
        raise ValueError(f"지원하지 않는 LLM_PROVIDER: {settings.LLM_PROVIDER}")

    open_jev_mode = getattr(settings, "OPEN_JEV_MODE", "off").lower()
    open_jev_endpoint = getattr(settings, "OPEN_JEV_ENDPOINT", "").strip()
    open_jev_model = getattr(settings, "OPEN_JEV_MODEL", "laya-router").strip().lower()
    if open_jev_mode in ("shadow", "active"):
        from core.llm.decision_provider import (
            HttpSystemOneDecisionProvider,
            LayaLocalDecisionProvider,
            ShadowDecisionRouter,
        )

        decision_provider = None
        if open_jev_endpoint:
            decision_provider = HttpSystemOneDecisionProvider(
                endpoint=open_jev_endpoint,
                model_id=open_jev_model,
            )
        elif open_jev_model.startswith("laya"):
            laya_variant = (
                "multilingual" if open_jev_model == "laya-multilingual"
                else "en" if open_jev_model == "laya-en"
                else "router"
            )
            decision_provider = LayaLocalDecisionProvider(mode=laya_variant)

        if decision_provider is not None:
            _cached_provider = ShadowDecisionRouter(
                llm_provider=base_provider,
                decision_provider=decision_provider,
                mode=open_jev_mode,
            )
            logger.info(
                f"LLM Provider 초기화 완료: Gemini + Open-Jev({decision_provider.model_name}, mode={open_jev_mode})"
            )
        else:
            _cached_provider = base_provider
    else:
        _cached_provider = base_provider

    return _cached_provider
