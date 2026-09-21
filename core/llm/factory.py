# core/llm/factory.py
from core.config import settings
from core.llm.base import LLMProvider

_cached_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """settings.LLM_PROVIDER 값만으로 provider를 교체할 수 있게 하는 팩토리."""
    global _cached_provider
    if _cached_provider is not None:
        return _cached_provider

    if settings.LLM_PROVIDER == "gemini":
        from core.llm.gemini_provider import GeminiProvider

        _cached_provider = GeminiProvider()
    else:
        raise ValueError(f"지원하지 않는 LLM_PROVIDER: {settings.LLM_PROVIDER}")

    return _cached_provider
