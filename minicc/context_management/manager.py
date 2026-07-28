"""High-level context orchestration around live model requests.

The API client owns transport and error classification.  This module owns every
policy decision and side effect that changes the working set: proactive
eviction/compaction, rejected-request recovery, lifecycle hooks, transcript
persistence, and per-conversation accounting.
"""

from enum import Enum

from minicc import hooks, sessions, ux

from . import budget, eviction, summary
from .budget import ContextState
from .summary import SummaryRuntime


# If still over budget after this many L4 compactions in a row, a single message
# is too large to compact away — stop and error instead of looping.
MAX_COMPACT_ATTEMPTS = 3
MAX_RECOVERY_COMPACTIONS = 8


class RecoveryKind(str, Enum):
    """Structural reason an API request needs a smaller context."""

    TOKENS = "tokens"
    BYTES = "bytes"


def _request_input_tokens(
    messages,
    *,
    model: str,
    system: str | None,
    tools,
    runtime: SummaryRuntime,
) -> int:
    """Estimate the complete tokenized input for the current live request."""
    input_tokens, _request_bytes = summary.live_request_footprint(
        messages,
        model=model,
        system=system,
        tools=tools,
        runtime=runtime,
    )
    return input_tokens


def _rebase_context(
    ctx: ContextState,
    messages,
    *,
    model: str,
    system: str | None,
    tools,
    runtime: SummaryRuntime,
) -> None:
    """Re-anchor state immediately after a successful in-memory rewrite."""
    ctx.rebase(
        messages,
        request_tokens=_request_input_tokens(
            messages,
            model=model,
            system=system,
            tools=tools,
            runtime=runtime,
        ),
        model=model,
    )


def compact(
    ctx: ContextState,
    messages,
    focus: str | None = None,
    model: str | None = None,
    system: str | None = None,
    tools=None,
    session_id: str | None = None,
    trigger: str = "auto",
    recovery: str | None = None,
    *,
    runtime: SummaryRuntime | None = None,
) -> bool | None:
    """Replace a safe old prefix with one structured summary message.

    The summary is produced before any working-set mutation.  For persistent
    sessions, the replacement state is written to the transcript before it is
    installed in memory, so a persistence failure cannot create a live-only
    compaction.

    Returns:
        ``True`` after a shrinking replacement, ``False`` when no safe/useful
        replacement exists, or ``None`` when ``PreCompact`` vetoes the attempt.

    ``recovery`` is ``"tokens"`` or ``"bytes"`` only after a rejected live
    request.  That path selects the largest safe prefix whose complete summary
    request fits the corresponding hard limit.
    """
    pre = hooks.run(
        "PreCompact",
        session_id=session_id,
        match_value=trigger,
        trigger=trigger,
        custom_instructions=focus or "",
    )
    hooks.surface(pre)
    hooks.raise_if_stopped(pre, "PreCompact")
    if pre.block:
        reason = pre.reason or "no reason given"
        ux.say(
            f"[compaction blocked by PreCompact hook: {reason}]",
            style=ux.S_INFO,
        )
        return None

    preferred_cut = summary._find_cut_index(messages)
    if preferred_cut is None:
        return False

    resolved = summary.resolve_runtime(runtime)
    chosen_model = model if model is not None else resolved.default_model
    required_savings = 0
    if recovery is None and trigger == "auto":
        request_tokens = _request_input_tokens(
            messages,
            model=chosen_model,
            system=system,
            tools=tools,
            runtime=resolved,
        )
        required_savings = max(
            0,
            ctx.context_size(
                messages,
                request_tokens=request_tokens,
                model=chosen_model,
            )
            - budget.effective_budget(chosen_model),
        )
    cut = summary._find_fitting_cut_index(
        messages,
        preferred_cut,
        chosen_model,
        recovery=recovery,
        focus=focus,
        system=system,
        tools=tools,
        required_savings_tokens=required_savings,
        runtime=resolved,
    )
    if cut is None:
        return False

    recent = messages[cut:]
    ux.say("[compacting conversation history...]", style=ux.S_INFO)
    # Summarize exactly the part being replaced. Keeping the raw tail out avoids
    # duplicated facts in the summary and keeps reactive recovery requests small.
    compact_summary = summary._summarize(
        messages[:cut],
        focus=focus,
        model=chosen_model,
        system=system,
        tools=tools,
        runtime=resolved,
    )
    if not compact_summary:
        ux.say("[compaction skipped: no summary produced]", style=ux.S_ERROR)
        return False

    # recent starts with an assistant message, so prepending a user summary keeps
    # valid role alternation without a synthetic assistant message.
    replacement = [
        {
            "role": "user",
            "content": f"[Earlier conversation summary]\n\n{compact_summary}",
        },
    ] + recent

    # Never let "compaction" grow the history.
    if budget.estimate_tokens(replacement) >= budget.estimate_tokens(messages):
        ux.say(
            "[compaction skipped: would not reduce the history]",
            style=ux.S_ERROR,
        )
        return False

    if session_id:
        sessions.log_compaction(session_id, replacement)

    messages[:] = replacement
    ctx.compactions += 1
    _rebase_context(
        ctx,
        messages,
        model=chosen_model,
        system=system,
        tools=tools,
        runtime=resolved,
    )
    ux.say(f"[compacted {cut} messages into a summary]", style=ux.S_INFO)

    post = hooks.run(
        "PostCompact",
        session_id=session_id,
        match_value=trigger,
        trigger=trigger,
        compact_summary=compact_summary,
    )
    hooks.surface(post)
    hooks.raise_if_stopped(post, "PostCompact")

    # Claude Code starts a compact-sourced lifecycle after every successful
    # compaction. Its additional context joins the newly compacted working set.
    if session_id:
        started = hooks.run(
            "SessionStart",
            session_id=session_id,
            match_value="compact",
            source="compact",
            model=chosen_model,
        )
        hooks.surface(started)
        hooks.raise_if_stopped(started, "SessionStart")
        for extra in started.additional_context:
            context_message = {"role": "user", "content": extra}
            sessions.append_message(session_id, context_message)
            messages.append(context_message)
            _rebase_context(
                ctx,
                messages,
                model=chosen_model,
                system=system,
                tools=tools,
                runtime=resolved,
            )
    return True


def prepare_for_request(
    ctx: ContextState,
    messages,
    *,
    model: str,
    system: str | None = None,
    tools=None,
    session_id: str | None = None,
    runtime: SummaryRuntime,
) -> None:
    """Apply proactive eviction or compaction immediately before a send.

    The size estimate covers the exact live request shape, including system
    blocks and tool schemas.  At most one edit class runs per call: over-budget
    requests compact; the lower band may evict old eligible tool results.
    """
    request_tokens = _request_input_tokens(
        messages,
        model=model,
        system=system,
        tools=tools,
        runtime=runtime,
    )
    size = ctx.context_size(
        messages,
        request_tokens=request_tokens,
        model=model,
    )
    token_budget = budget.effective_budget(model)
    if size > token_budget:
        # Compaction replaces the old prefix anyway, so evicting first would
        # break its warm prompt-cache prefix without helping the summary call.
        if ctx.compact_attempts >= MAX_COMPACT_ATTEMPTS:
            ux.say(
                "[Autocompact is thrashing: still over budget after "
                f"{MAX_COMPACT_ATTEMPTS} compactions. An individual message, "
                "system prompt, or advertised tool schema is likely too large. "
                "Try /clear, reduce the prefix/tool set, or read large files in "
                "smaller chunks (offset/limit).]",
                style=ux.S_ERROR,
            )
            raise RuntimeError("compact thrashing")
        result = compact(
            ctx,
            messages,
            model=model,
            system=system,
            tools=tools,
            session_id=session_id,
            runtime=runtime,
        )
        # A hook veto is the user's choice, not a failed compaction attempt.
        if result is not None:
            ctx.compact_attempts += 1
        return

    ctx.compact_attempts = 0
    if size <= eviction.TOOL_RESULT_EVICTION_TRIGGER_TOKENS:
        return

    eviction_kwargs = {}
    if session_id:
        eviction_kwargs["session_id"] = session_id
    evicted = eviction.evict_old_tool_results(messages, **eviction_kwargs)
    if not evicted:
        return

    ctx.record_eviction(evicted)
    _rebase_context(
        ctx,
        messages,
        model=model,
        system=system,
        tools=tools,
        runtime=runtime,
    )
    count = getattr(evicted, "count", evicted)
    saved = getattr(evicted, "estimated_tokens_saved", 0)
    ux.say(
        f"[evicted {count} old tool_results"
        + (
            f", reclaiming ~{saved:,} tokens]"
            if saved
            else " to reclaim context]"
        ),
        style=ux.S_INFO,
    )


def recover_rejected_context(
    ctx: ContextState,
    messages,
    *,
    recovery: RecoveryKind,
    model: str,
    system: str | None = None,
    tools=None,
    session_id: str | None = None,
    runtime: SummaryRuntime,
) -> bool:
    """Shrink a structurally rejected context enough for one live retry.

    The caller classifies the transport error and retains ownership of retrying
    or re-raising it.  This function returns ``True`` only when the complete
    rewritten request—not just ``messages``—is below the relevant safety line.
    """
    ux.say(
        "[request rejected — shrinking context and retrying]",
        style=ux.S_ERROR,
    )
    eviction_kwargs = {
        "min_savings_tokens": 0,
        "keep_recent": 0,
        "allow_lossy_fallback": True,
    }
    if session_id:
        eviction_kwargs["session_id"] = session_id
    evicted = eviction.evict_old_tool_results(
        messages,
        **eviction_kwargs,
    )
    if evicted:
        ctx.record_eviction(evicted)
        _rebase_context(
            ctx,
            messages,
            model=model,
            system=system,
            tools=tools,
            runtime=runtime,
        )

    # A rejected request cannot protect recent results. If eviction alone does
    # not make the rewritten request safe, summarize bounded prefixes until it
    # does, without ever issuing another predictably oversized live request.
    must_compact = not evicted
    recovery_compactions = 0
    while must_compact or summary.recovery_needed(
        messages,
        model,
        recovery.value,
        system=system,
        tools=tools,
        runtime=runtime,
    ):
        if recovery_compactions >= MAX_RECOVERY_COMPACTIONS:
            return False
        result = compact(
            ctx,
            messages,
            model=model,
            system=system,
            tools=tools,
            session_id=session_id,
            recovery=recovery.value,
            runtime=runtime,
        )
        if not result:
            return False
        recovery_compactions += 1
        must_compact = False
    return True


def recap(
    messages,
    focus: str | None = None,
    *,
    runtime: SummaryRuntime | None = None,
) -> str:
    """Return a display-only recap without changing history or context state."""
    if len(messages) < 2:
        return "(nothing to recap yet)"
    return (
        summary._summarize(messages, focus=focus, runtime=runtime)
        or "(no summary produced)"
    )


def context_usage(
    ctx: ContextState,
    messages,
    model: str | None = None,
    system: str | None = None,
    tools=None,
    *,
    runtime: SummaryRuntime | None = None,
) -> dict:
    """Return full-request usage estimates and cumulative edit telemetry."""
    resolved = summary.resolve_runtime(runtime)
    if model is None:
        model = resolved.default_model
    request_tokens = _request_input_tokens(
        messages,
        model=model,
        system=system,
        tools=tools,
        runtime=resolved,
    )
    tokens = ctx.context_size(
        messages,
        request_tokens=request_tokens,
        model=model,
    )
    token_budget = budget.effective_budget(model)
    pct = (tokens / token_budget * 100) if token_budget else 0

    tool_results = 0
    evicted = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_results += 1
                if eviction._is_evicted_content(block.get("content")):
                    evicted += 1

    return {
        "estimated_tokens": tokens,
        "budget": token_budget,
        "pct_of_budget": pct,
        "messages": len(messages),
        "tool_results": tool_results,
        "evicted": evicted,
        "eviction_events": ctx.evictions,
        "evicted_tool_results": ctx.evicted_tool_results,
        "evicted_tokens": ctx.evicted_tokens,
        "last_eviction_suffix_tokens": ctx.last_eviction_suffix_tokens,
        "compaction_events": ctx.compactions,
    }
