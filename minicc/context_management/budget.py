"""Token-budget policy and per-conversation context accounting.

This module owns arithmetic, not request orchestration.  Callers provide the
estimated token size of the complete request input when they have it; the state
then anchors that estimate to the API's last real input-token usage.
"""

import json
import os
from dataclasses import dataclass


# Context window (input tokens) by model-id prefix; ids may carry a date suffix.
_MODEL_WINDOWS = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-mythos-preview": 1_000_000,
}
# Conservative fallback for an unmapped model.
_DEFAULT_WINDOW = 200_000

# Compact once the next request would come within this many tokens of the
# model's context window (i.e. the budget is window minus this headroom).
COMPACT_BUFFER_TOKENS = 13_000
MAX_OUTPUT_TOKENS = 16_000


@dataclass
class ContextState:
    """Mutable sizing and edit telemetry for exactly one conversation.

    A successful request supplies the most reliable baseline.  After a local
    rewrite, ``rebase`` advances that baseline with an estimated delta until the
    next successful request replaces it with real usage again.
    """

    last_input_tokens: int = 0  # Anchored total: real after send, predicted after edit.
    last_message_tokens: int | None = None  # Messages estimate matching the anchor.
    last_request_tokens: int | None = None  # Full-input estimate matching the anchor.
    last_model: str | None = None  # Model whose tokenizer produced the anchor.
    compact_attempts: int = 0  # Consecutive over-budget compaction attempts.
    evictions: int = 0  # Applied eviction batches.
    compactions: int = 0  # Successful full compactions.
    evicted_tool_results: int = 0  # Total tool-result blocks cleared.
    evicted_tokens: int = 0  # Estimated net tokens reclaimed by eviction.
    last_eviction_suffix_tokens: int = 0  # Latest invalidated cache suffix.

    def context_size(
        self,
        messages,
        *,
        request_tokens: int | None = None,
        model: str | None = None,
    ) -> int:
        """Predict the current input size from the last real API measurement.

        ``request_tokens`` should estimate the complete input payload
        (system, tools, and messages).  A model change invalidates real usage
        measured by the old tokenizer, so the current local estimate becomes a
        cold baseline.  When unavailable, the method falls back to the
        messages-only delta used by older callers.
        """
        message_tokens = estimate_tokens(messages)
        if model is not None and self.last_model not in (None, model):
            return request_tokens if request_tokens is not None else message_tokens
        if self.last_message_tokens is None:
            return request_tokens if request_tokens is not None else message_tokens
        if request_tokens is not None and self.last_request_tokens is not None:
            estimated_delta = request_tokens - self.last_request_tokens
        else:
            estimated_delta = message_tokens - self.last_message_tokens
        return max(0, self.last_input_tokens + estimated_delta)

    def record_input(
        self,
        total_input_tokens: int,
        message_tokens: int,
        request_tokens: int | None = None,
        model: str | None = None,
    ) -> None:
        """Anchor a successful request's real usage and matching estimates."""
        self.last_input_tokens = total_input_tokens
        self.last_message_tokens = message_tokens
        self.last_request_tokens = request_tokens
        self.last_model = model

    def rebase(
        self,
        messages,
        *,
        request_tokens: int | None = None,
        model: str | None = None,
    ) -> None:
        """Promote a rewritten working set's prediction to the new baseline."""
        predicted = self.context_size(
            messages,
            request_tokens=request_tokens,
            model=model,
        )
        self.last_input_tokens = predicted
        self.last_message_tokens = estimate_tokens(messages)
        self.last_request_tokens = request_tokens
        self.last_model = model

    def reset_size(self) -> None:
        """Forget a stale request baseline after history shrinks externally."""
        self.last_input_tokens = 0
        self.last_message_tokens = None
        self.last_request_tokens = None
        self.last_model = None

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


def estimate_request_input_tokens(params: dict) -> int:
    """Estimate tokenized input fields from one assembled Messages request.

    Output controls such as ``max_tokens`` and ``thinking`` affect request
    bytes, but the tokenized input is the rendered tools, system, and messages.
    """
    input_payload = {
        key: params[key]
        for key in ("system", "tools", "messages")
        if key in params
    }
    return estimate_tokens(input_payload)


def _json_default(value):
    """Serialize SDK models while keeping unknown values measurable."""
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
    """Return the known window or a compatible override for an unknown model.

    Claude Code applies ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` directly to model
    names it does not recognize, which matters for Anthropic-compatible custom
    endpoints.  Known Claude models retain their published capacity.
    """
    for prefix, window in _MODEL_WINDOWS.items():
        if model.startswith(prefix):
            return window
    raw = os.getenv("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "")
    try:
        override = int(raw)
    except ValueError:
        return _DEFAULT_WINDOW
    if override > 0:
        return override
    return _DEFAULT_WINDOW


def _compaction_capacity(model: str) -> int:
    """Return the model window used for proactive compaction calculations."""
    model_window = _model_window(model)
    # Read per call so the environment override is not frozen at module import.
    raw = os.getenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "")
    try:
        override = int(raw)
    except ValueError:
        return model_window
    if override <= 0:
        return model_window
    return min(model_window, override)


def resolve_max_output_tokens() -> int:
    """Resolve the live request ceiling from Claude Code's compatible override.

    Invalid and non-positive values fall back to minicc's 16K default.  Provider
    and model-specific hard caps remain the API's responsibility.
    """
    raw = os.getenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "")
    try:
        override = int(raw)
    except ValueError:
        return MAX_OUTPUT_TOKENS
    return override if override > 0 else MAX_OUTPUT_TOKENS


def effective_budget(
    model: str,
    max_output_tokens: int | None = None,
) -> int:
    """Return the proactive compaction threshold for ``model``.

    The threshold reserves the same output ceiling sent to the API, then leaves
    an additional 13K safety buffer.  Claude Code-compatible window and
    percentage environment overrides are read on every call.
    """
    capacity = _compaction_capacity(model)
    output_reserve = (
        resolve_max_output_tokens()
        if max_output_tokens is None
        else max(0, max_output_tokens)
    )
    default_budget = max(
        0,
        capacity - output_reserve - COMPACT_BUFFER_TOKENS,
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
