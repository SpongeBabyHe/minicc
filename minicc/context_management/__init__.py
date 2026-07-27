"""Context-management subsystem: budgets, eviction, summaries, and orchestration."""

from .budget import (
    COMPACT_BUFFER_TOKENS,
    MAX_OUTPUT_TOKENS,
    ContextState,
    effective_budget,
    estimate_tokens,
)
from .eviction import (
    COMPACTABLE_TOOL_NAMES,
    EVICTED_MARKER,
    TOOL_RESULT_EVICTION_KEEP_RECENT,
    TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS,
    TOOL_RESULT_EVICTION_TRIGGER_TOKENS,
    EvictionPlan,
    EvictionResult,
    ToolResultEdit,
    apply_tool_result_eviction,
    evict_old_tool_results,
    plan_tool_result_eviction,
)
from .manager import (
    MAX_COMPACT_ATTEMPTS,
    MAX_RECOVERY_COMPACTIONS,
    compact,
    context_usage,
    recap,
)
from .summary import SummaryRuntime, recovery_needed


__all__ = [
    "COMPACTABLE_TOOL_NAMES",
    "COMPACT_BUFFER_TOKENS",
    "EVICTED_MARKER",
    "MAX_COMPACT_ATTEMPTS",
    "MAX_OUTPUT_TOKENS",
    "MAX_RECOVERY_COMPACTIONS",
    "TOOL_RESULT_EVICTION_KEEP_RECENT",
    "TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS",
    "TOOL_RESULT_EVICTION_TRIGGER_TOKENS",
    "ContextState",
    "EvictionPlan",
    "EvictionResult",
    "SummaryRuntime",
    "ToolResultEdit",
    "apply_tool_result_eviction",
    "compact",
    "context_usage",
    "effective_budget",
    "estimate_tokens",
    "evict_old_tool_results",
    "plan_tool_result_eviction",
    "recap",
    "recovery_needed",
]
