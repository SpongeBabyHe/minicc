"""Context sizing, model-window policy, and per-conversation trigger state."""

import json
import os
from dataclasses import dataclass


# Context window (input tokens) by model-id prefix; ids may carry a date suffix.
_MODEL_WINDOWS = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-fable-5": 1_000_000,
}
# Conservative fallback for an unmapped model.
_DEFAULT_WINDOW = 200_000

# Compact once the next request would come within this many tokens of the
# model's context window (i.e. the budget is window minus this headroom).
COMPACT_BUFFER_TOKENS = 13_000
MAX_OUTPUT_TOKENS = 16_000


@dataclass
class ContextState:
    """Trigger state owned by one conversation, never shared across loops."""

    last_input_tokens: int = 0  # Last request's real total input tokens.
    last_message_tokens: int | None = None  # Message estimate for that request.
    compact_attempts: int = 0  # Consecutive over-budget compaction attempts.
    evictions: int = 0  # Applied eviction batches.
    compactions: int = 0  # Successful full compactions.
    evicted_tool_results: int = 0  # Total tool-result blocks cleared.
    evicted_tokens: int = 0  # Estimated net tokens reclaimed by eviction.
    last_eviction_suffix_tokens: int = 0  # Latest invalidated cache suffix.

    def context_size(self, messages) -> int:
        """Predict the next input from the real baseline plus message growth."""
        message_tokens = estimate_tokens(messages)
        if self.last_message_tokens is None:
            return message_tokens
        return max(
            0,
            self.last_input_tokens + message_tokens - self.last_message_tokens,
        )

    def record_input(self, total_input_tokens: int, message_tokens: int) -> None:
        """Anchor one successful request's real usage and message estimate."""
        self.last_input_tokens = total_input_tokens
        self.last_message_tokens = message_tokens

    def rebase(self, messages) -> None:
        """Make the current prediction the baseline after an in-place rewrite."""
        predicted = self.context_size(messages)
        self.last_input_tokens = predicted
        self.last_message_tokens = estimate_tokens(messages)

    def reset_size(self) -> None:
        """Forget a stale request baseline after history shrinks externally."""
        self.last_input_tokens = 0
        self.last_message_tokens = None

    def record_eviction(self, result) -> None:
        """Accumulate one applied local context edit for ``/context``."""
        count = getattr(result, "count", result)
        if not count:
            return
        self.evictions += 1
        self.evicted_tool_results += int(count)
        self.evicted_tokens += int(
            getattr(result, "estimated_tokens_saved", 0)
        )
        self.last_eviction_suffix_tokens = int(
            getattr(result, "invalidated_suffix_tokens", 0)
        )


def estimate_tokens(messages) -> int:
    """Rough token count from serialized UTF-8 bytes (~4 bytes per token).

    The same representation feeds every local sizing decision, so context
    triggers and eviction savings cannot disagree badly on non-ASCII content.
    Invalid or cyclic message structures raise instead of looking empty.
    """
    return _serialized_size(messages) // 4


def _json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return str(value)


def _serialized_size(value) -> int:
    """Approximate request bytes for an API-shaped value."""
    return len(
        json.dumps(
            value,
            default=_json_default,
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _model_window(model: str) -> int:
    """Context window for a model id, tolerating a date suffix."""
    for prefix, window in _MODEL_WINDOWS.items():
        if model.startswith(prefix):
            return window
    return _DEFAULT_WINDOW


def _compaction_capacity(model: str) -> int:
    """Model window, optionally lowered by Claude Code's compatible override."""
    model_window = _model_window(model)
    raw = os.getenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "")
    try:
        override = int(raw)
    except ValueError:
        return model_window
    if override <= 0:
        return model_window
    return min(model_window, override)


def effective_budget(
    model: str, max_output_tokens: int = MAX_OUTPUT_TOKENS
) -> int:
    """Auto-compaction threshold after output reserve, buffer, and overrides."""
    capacity = _compaction_capacity(model)
    default_budget = max(
        0,
        capacity - max_output_tokens - COMPACT_BUFFER_TOKENS,
    )
    raw_pct = os.getenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "")
    try:
        percentage = int(raw_pct)
    except ValueError:
        return default_budget
    if not 1 <= percentage <= 100:
        return default_budget
    # The override can compact earlier, never later than the default threshold.
    return min(default_budget, capacity * percentage // 100)
