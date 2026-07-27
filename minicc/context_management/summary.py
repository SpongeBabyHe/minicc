"""Safe-cut planning and cache-compatible conversation summarization."""

from dataclasses import dataclass
from typing import Callable

from .budget import (
    _model_window,
    _serialized_size,
    effective_budget,
    estimate_tokens,
)


KEEP_RECENT_MESSAGES_TARGET = 6

# The normal summary is intentionally small. If it is truncated or tries to use
# a tool, one guarded retry gets more room and disables tool use explicitly.
_SUMMARY_MAX_TOKENS = 2048
_SUMMARY_RETRY_MAX_TOKENS = 8192

# A 413 is a 32 MiB request-byte rejection. The emergency summary's complete
# serialized params must fit below 24 MiB, leaving 8 MiB for wire/envelope drift.
_SUMMARY_REQUEST_BYTE_BUDGET = 24 * 1024 * 1024

_COMPACT_PROMPT = """You compress an agent's conversation history into a structured summary so work can continue with less context. Match Claude Code's 9-section shape. Output exactly:

## Primary Request and Intent
<the user's explicit requests and overall intent; keep wording where it matters>

## Key Technical Concepts
- <important technologies, patterns, conventions in play>

## Files and Code Sections
- <files read/edited/created, why each matters, key snippets>

## Errors and fixes
- <errors hit and how each was resolved>

## Problem Solving
<problems solved and any ongoing troubleshooting>

## All user messages
- <every non-tool user message, so intent isn't lost>

## Pending Tasks
- <explicitly requested work not yet done>

## Current Work
<what was being done right before this summary, with file paths>

## Optional Next Step
<the next step, tightly tied to the most recent work>

Be specific (file paths, decisions). Do not call tools; respond with text only.
No pleasantries."""


@dataclass(frozen=True)
class SummaryRuntime:
    """Narrow LLM adapter required by summary planning and execution."""

    default_model: str
    default_tools: object
    build_system_block: Callable
    cacheable: Callable
    thinking_param: Callable
    create_message: Callable
    record_usage: Callable


def resolve_runtime(runtime: SummaryRuntime | None = None) -> SummaryRuntime:
    """Use an injected runtime, with a lazy compatibility fallback to llm."""
    if runtime is not None:
        return runtime
    from minicc import llm

    return llm.summary_runtime()


def _safe_cut_indices(messages) -> list[int]:
    """All non-empty-prefix cuts whose kept tail starts with an assistant."""
    return [
        index
        for index in range(1, len(messages))
        if messages[index].get("role") == "assistant"
    ]


def _find_cut_index(messages) -> int | None:
    """Pick the safe cut nearest the recent-tail target.

    The kept tail must start with an assistant message, so prepending the summary
    as a single user message keeps the user/assistant turns alternating. Cutting
    before an assistant also keeps every tool_use together with its tool_result
    in the tail — the API rejects a tool_use whose result was summarized away.

    This judges structure only, not whether compaction is worthwhile. Prefer a
    boundary that keeps at most the target number of recent messages; if none
    exists there, fall back to the latest earlier boundary.
    """
    safe_cuts = _safe_cut_indices(messages)
    if not safe_cuts:
        return None
    target = max(1, len(messages) - KEEP_RECENT_MESSAGES_TARGET)
    return next(
        (cut for cut in safe_cuts if cut >= target),
        safe_cuts[-1],
    )


def _summary_instruction(focus: str | None = None) -> dict:
    focus_line = f"\n\nFocus the summary on: {focus}" if focus else ""
    return {"role": "user", "content": _COMPACT_PROMPT + focus_line}


def _summary_params(
    messages,
    *,
    focus: str | None = None,
    model: str | None = None,
    system: str | None = None,
    tools=None,
    max_tokens: int = _SUMMARY_MAX_TOKENS,
    disable_tools: bool = False,
    runtime: SummaryRuntime | None = None,
) -> dict:
    """Build the exact cache-compatible request used by the summarizer."""
    resolved = resolve_runtime(runtime)
    chosen_model = model if model is not None else resolved.default_model
    params = {
        "model": chosen_model,
        "max_tokens": max_tokens,
        "system": resolved.build_system_block(system),
        "tools": tools if tools is not None else resolved.default_tools,
        "messages": resolved.cacheable(
            list(messages) + [_summary_instruction(focus)]
        ),
    }
    thinking = resolved.thinking_param(chosen_model)
    if thinking:
        params["thinking"] = thinking
    if disable_tools:
        params["tool_choice"] = {"type": "none"}
    return params


def _summary_request_footprint(
    messages,
    *,
    focus: str | None,
    model: str,
    system: str | None,
    tools,
    runtime: SummaryRuntime | None = None,
) -> tuple[int, int]:
    """Estimated input tokens and bytes for the largest summary retry."""
    params = _summary_params(
        messages,
        focus=focus,
        model=model,
        system=system,
        tools=tools,
        max_tokens=_SUMMARY_RETRY_MAX_TOKENS,
        disable_tools=True,
        runtime=runtime,
    )
    input_payload = {
        "system": params["system"],
        "tools": params["tools"],
        "messages": params["messages"],
    }
    return estimate_tokens(input_payload), _serialized_size(params)


def _find_fitting_cut_index(
    messages,
    preferred: int,
    model: str,
    recovery: str | None = None,
    *,
    focus: str | None = None,
    system: str | None = None,
    tools=None,
    required_savings_tokens: int = 0,
    runtime: SummaryRuntime | None = None,
    request_byte_budget: int | None = None,
) -> int | None:
    """Choose a safe cut whose complete summary request fits hard limits.

    Normal compaction keeps as much raw tail as possible while selecting enough
    prefix to cover the required saving. Recovery chooses the largest fitting
    prefix because the live request has already been rejected.
    """
    if request_byte_budget is None:
        request_byte_budget = _SUMMARY_REQUEST_BYTE_BUDGET
    max_summary_input_tokens = max(
        0,
        _model_window(model) - _SUMMARY_RETRY_MAX_TOKENS,
    )
    max_summary_request_bytes = (
        request_byte_budget if recovery == "bytes" else None
    )
    safe_cuts = _safe_cut_indices(messages)
    if not safe_cuts:
        return None

    footprints: dict[int, tuple[int, int]] = {}

    def fits(cut: int) -> bool:
        if cut not in footprints:
            footprints[cut] = _summary_request_footprint(
                messages[:cut],
                focus=focus,
                model=model,
                system=system,
                tools=tools,
                runtime=runtime,
            )
        input_tokens, request_bytes = footprints[cut]
        if input_tokens > max_summary_input_tokens:
            return False
        return (
            max_summary_request_bytes is None
            or request_bytes <= max_summary_request_bytes
        )

    if recovery:
        return next((cut for cut in reversed(safe_cuts) if fits(cut)), None)

    at_or_after_target = [cut for cut in safe_cuts if cut >= preferred]
    fitting = []
    required_savings = max(0, required_savings_tokens)
    for cut in at_or_after_target:
        if not fits(cut):
            continue
        fitting.append(cut)
        if required_savings == 0:
            return cut
        conservative_savings = max(
            0,
            estimate_tokens(messages[:cut]) - _SUMMARY_RETRY_MAX_TOKENS,
        )
        if conservative_savings >= required_savings:
            return cut

    if fitting:
        return fitting[-1]

    # A very large preferred prefix may not fit in one summary request. Move the
    # cut backward and retain more than the target rather than refusing outright.
    return next(
        (
            cut
            for cut in reversed(safe_cuts)
            if cut < preferred and fits(cut)
        ),
        None,
    )


def recovery_needed(
    messages,
    model: str,
    recovery: str,
    *,
    request_byte_budget: int | None = None,
    effective_budget_fn=effective_budget,
) -> bool:
    """Whether a rewritten live request is still predictably over its safe line."""
    if request_byte_budget is None:
        request_byte_budget = _SUMMARY_REQUEST_BYTE_BUDGET
    if recovery == "bytes":
        return _serialized_size(messages) > request_byte_budget
    return estimate_tokens(messages) > effective_budget_fn(model)


def _block_attr(block, name: str):
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _summary_text(response) -> str | None:
    """Accept only a complete, text-only summary response."""
    if getattr(response, "stop_reason", None) not in ("end_turn", "stop_sequence"):
        return None
    block_types = [_block_attr(block, "type") for block in response.content]
    if any(
        block_type == "tool_use"
        or (isinstance(block_type, str) and block_type.endswith("_tool_use"))
        for block_type in block_types
    ):
        return None
    text = "".join(
        str(_block_attr(block, "text") or "")
        for block in response.content
        if _block_attr(block, "type") == "text"
    ).strip()
    return text or None


def _summarize(
    messages,
    focus: str | None = None,
    model: str | None = None,
    system: str | None = None,
    tools=None,
    runtime: SummaryRuntime | None = None,
) -> str | None:
    """Return a complete text summary, retrying one malformed response.

    The same model, system, tools, and thinking mode preserve every cacheable
    prefix layer. Tools remain advertised on the first call for that reason, but
    the instruction forbids using them. A tool call, empty response, or truncated
    response gets one retry with tool_choice=none and a larger output allowance.
    """
    resolved = resolve_runtime(runtime)
    params = _summary_params(
        messages,
        focus=focus,
        model=model,
        system=system,
        tools=tools,
        runtime=resolved,
    )

    for attempt in range(2):
        if attempt:
            params["max_tokens"] = _SUMMARY_RETRY_MAX_TOKENS
            params["tool_choice"] = {"type": "none"}
        response = resolved.create_message(**params)
        resolved.record_usage(response.usage)
        summary = _summary_text(response)
        if summary:
            return summary
        if getattr(response, "stop_reason", None) == "refusal":
            break
    return None
