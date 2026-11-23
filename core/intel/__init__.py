# core/intel/__init__.py
"""공유 인텔리전스 계층 (Shared Intelligence Layer).

마스터 인텔리전스 데몬(intel-master)이 수집·중복제거·분석한 뉴스 사건 노드,
증권사 리포트, 인사이트 배치 및 LLM 토큰 사용량 원장을 단일 SQLite WAL DB
(`data/jarvis_intel.db`)에 영속화하고, 모든 계좌 엔진이 읽기 참조할 수 있도록 한다.
"""
from core.intel.db import (
    get_intel_connection,
    init_intel_db,
)
from core.intel.event_deduper import (
    EventDeduper,
    EventNodeMatch,
    build_cluster_alias_key,
    build_url_alias_key,
    extract_normalized_tokens,
)
from core.intel.usage_tracker import (
    BudgetExceededError,
    BudgetStatus,
    UsageTracker,
    estimate_cost_usd,
)

__all__ = [
    "BudgetExceededError",
    "BudgetStatus",
    "EventDeduper",
    "EventNodeMatch",
    "UsageTracker",
    "build_cluster_alias_key",
    "build_url_alias_key",
    "estimate_cost_usd",
    "extract_normalized_tokens",
    "get_intel_connection",
    "init_intel_db",
]
