"""High-level context compaction, lifecycle hooks, and usage reporting."""

from minicc import hooks, sessions, ux

from . import budget, eviction, summary
from .budget import ContextState
from .summary import SummaryRuntime


# If still over budget after this many L4 compactions in a row, a single message
# is too large to compact away — stop and error instead of looping.
MAX_COMPACT_ATTEMPTS = 3
MAX_RECOVERY_COMPACTIONS = 8


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
    """Summarize older messages into one and replace them in place.

    Returns True if it shrank the history; False if it was attempted but could
    not; None if a PreCompact hook vetoed it. A veto does not count toward the
    thrash guard.

    recovery is "tokens" or "bytes" only after a rejected live request. In that
    path, the planner chooses the largest safe prefix whose complete summary
    request fits, so it never repeats the same over-limit request.
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
        required_savings = max(
            0,
            ctx.context_size(messages) - budget.effective_budget(chosen_model),
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

    messages[:] = replacement
    ctx.compactions += 1
    if session_id:
        sessions.log_compaction(session_id, messages)
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
            messages.append(context_message)
            sessions.append_message(session_id, context_message)
    ctx.rebase(messages)
    return True


def recap(
    messages,
    focus: str | None = None,
    *,
    runtime: SummaryRuntime | None = None,
) -> str:
    """Summarize the conversation for display without changing its history."""
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
    *,
    runtime: SummaryRuntime | None = None,
) -> dict:
    """Return the current context budget, composition, and edit counters."""
    if model is None:
        model = summary.resolve_runtime(runtime).default_model
    tokens = ctx.context_size(messages)
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
