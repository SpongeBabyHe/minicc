"""Context management — budgets, eviction, compaction (CC's services/compact/).

CC keeps this machinery in a dedicated module tree (`services/compact/`:
autoCompact / microCompact / reactiveCompact / postCompactCleanup …), a PEER of
the API client (`services/api/`), with the trigger sequence run around the model
call in the query pipeline — the client itself is compaction-free (source-read
2026-07-21; docs/CC_CONTEXT_MANAGEMENT.md §7). minicc mirrors that split:
llm.py is the client, this module is the policy. For now llm_response still
orchestrates the trigger (R1a); moving it up to the engine layer is deferred to
the background-tier loop rewrite (R1b — CC's query.ts placement).

The layers (CONTEXT_MANAGEMENT.md):
  L3  incremental tool_result eviction (CC context-editing / clear_tool_uses)
  L4  LLM compaction near the window (CC autoCompact; prefix-sharing summary)
  L5  thrash guard (CC circuit breaker: stop after ~3 failed compactions)

State is PER CONVERSATION (`ContextState`), not module-global: the main session
owns one, every agent_loop (sub-agents, /memory consolidate) creates its own,
and a bare single-shot llm_response gets a throwaway. The pre-R1 module-global
state let an in-process sub-agent overwrite the parent's trigger input
(executed repro: parent 950K read as 3K → proactive compaction silently
skipped) and let a large parent poison a fresh sub-agent's first turn. Teams
processes and coordinator cost attribution need per-agent state anyway.
"""

import json
from dataclasses import dataclass

from minicc import hooks, sessions, ux

# ─── L4: compaction trigger (window-relative, like Claude Code) ─────────────
# CC auto-compacts near the model's context window; its leaked default is
# `effectiveContextWindow - 13K`. minicc mirrors that exact shape. (The old
# `min(95%, 350K)` clamp was dropped: the "~450K wall" in PAIN.md was a
# misdiagnosed ITPM rate limit, not a request-size ceiling. Rate limits are
# handled by SDK backoff + llm.py's reactive-429 fallback, not by shrinking
# the budget. See docs/CC_ALIGNMENT_PLAN.md.)
COMPACT_BUFFER_TOKENS = 13_000

# Context window (input tokens) by model-id prefix; ids may carry a date suffix.
_MODEL_WINDOWS = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-fable-5": 1_000_000,
}
_DEFAULT_WINDOW = 200_000          # conservative fallback for an unmapped model

# ─── L3: incremental tool_result eviction (CC's context-editing) ────────────
# Above CLEAR_TRIGGER (but below the compaction budget) minicc blanks the oldest
# tool_result contents each turn — CC's `clear_tool_uses`. `clear_at_least` guards
# the prompt cache: in-place eviction rewrites mid-history and breaks the cache, so
# only do it when it frees at least CLEAR_AT_LEAST tokens (worth the re-write).
# Mirrors CC's trigger / keep / clear_at_least.
CLEAR_TRIGGER = 100_000             # start clearing above this (context-editing trigger default = 100K)
CLEAR_AT_LEAST = 5_000             # ...but only if it frees ≥ this (don't nibble the cache)
RECENT_TOOL_RESULTS_KEEP = 3       # recent tool_results kept intact (context-editing keep default = 3)
EVICTED_MARKER = (
    "[content omitted; was earlier in conversation — re-call the tool if needed]"
)

# ─── L4: how much recent history survives a compaction verbatim ─────────────
# The actual cut lands on an assistant-message boundary at or after this point
# (see _find_cut_index), keeping tool_use/tool_result pairs intact.
KEEP_RECENT_MESSAGES = 6

# ─── L5: thrashing guard ────────────────────────────────────────────────────
# If still over budget after this many compactions in a row, a single message
# is too large to compact away — stop and error instead of looping.
MAX_COMPACT_ATTEMPTS = 3

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

Be specific (file paths, decisions). No pleasantries."""


@dataclass
class ContextState:
    """Per-conversation trigger state. One per agent loop: the main session's
    lives in cli.main (rotated on /clear), each sub-agent spawn gets a fresh
    one, and a bare single-shot llm_response call gets a throwaway — so nested
    calls can never poison another conversation's trigger.

    `last_input_tokens` is the real input size of the LAST request in THIS
    conversation (from response.usage) — the compaction trigger compares
    against it rather than a char-estimate: accurate, one turn stale (the
    headroom absorbs that). Also the basis CC's /context reports.
    `evictions`/`compactions` are durable per-conversation counters surfaced
    by /context, so you can tell whether L3/L4 fired without hunting for dim
    log lines. (Per-agent usage accounting for coordinator cost attribution
    belongs here too — deferred to the teams tier.)"""

    last_input_tokens: int = 0
    compact_attempts: int = 0
    evictions: int = 0
    compactions: int = 0

    def context_size(self, messages) -> int:
        """Best read of the next request's input size: the last response's REAL
        usage, or the char-estimate on the cold first turn before any."""
        return self.last_input_tokens or estimate_tokens(messages)

    def reset_size(self) -> None:
        """Forget the last request's real size; fall back to the char-estimate
        until the next response. Called when the history shrinks OUTSIDE the
        normal flow (conversation /rewind) — otherwise the stale large value
        would trigger a spurious compaction on the now-small history."""
        self.last_input_tokens = 0


def estimate_tokens(messages) -> int:
    """Rough token estimate, ~4 chars per token.

    Uses JSON serialization to handle both dict-form messages and the
    Anthropic SDK's Block objects. Overestimates slightly (JSON overhead +
    repr of objects), which is fine for trigger decisions — evicting a turn
    earlier than strictly needed is better than blowing the budget.
    """
    try:
        return len(json.dumps(messages, default=str)) // 4
    except Exception:
        return 0


def _model_window(model: str) -> int:
    """Context window (tokens) for a model id (tolerates a date suffix)."""
    for prefix, window in _MODEL_WINDOWS.items():
        if model.startswith(prefix):
            return window
    return _DEFAULT_WINDOW


def effective_budget(model: str) -> int:
    """Compaction threshold: the model window minus a safety buffer, matching CC's
    `effectiveContextWindow - 13K`. No sub-window clamp (see COMPACT_BUFFER_TOKENS)."""
    return _model_window(model) - COMPACT_BUFFER_TOKENS


def evict_old_tool_results(messages, min_free: int = 0) -> int:
    """Replace `content` of old tool_result blocks with EVICTED_MARKER.

    Keeps the RECENT_TOOL_RESULTS_KEEP most recent intact. Returns the count of
    blocks evicted (0 if none). Mutates `messages` in place.

    `min_free` is the `clear_at_least` guard: if the eviction would free fewer
    than `min_free` estimated tokens, skip it entirely (return 0) — don't break the
    prompt cache for a gain too small to be worth the re-write. This is what makes
    per-turn incremental eviction cache-safe (CC's `clear_at_least`).

    Conversation structure (assistant tool_use → user tool_result) is preserved;
    the model still sees the tool was called and can re-call if needed.
    """
    candidates = []
    for i, msg in enumerate(messages):
        content = msg["content"]
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            if block.get("content") == EVICTED_MARKER:
                continue
            candidates.append((i, j))
    if len(candidates) <= RECENT_TOOL_RESULTS_KEEP:
        return 0
    to_evict = candidates[:-RECENT_TOOL_RESULTS_KEEP]
    # clear_at_least: only break the cache if this frees enough to be worth it.
    freed = sum(len(str(messages[i]["content"][j].get("content", ""))) for i, j in to_evict) // 4
    if freed < min_free:
        return 0
    for i, j in to_evict:
        messages[i]["content"][j]["content"] = EVICTED_MARKER
    return len(to_evict)


def _find_cut_index(messages) -> int | None:
    """Find a safe cut point: summarize messages[:cut], keep messages[cut:].

    The tail must start with an ASSISTANT message. Then prepending a single
    [user: summary] yields valid alternation (user:summary -> assistant ->
    user -> ...). Cutting before an assistant keeps every tool_use paired with
    its following tool_result inside the tail — the API requires each tool_use
    to have its tool_result in the next message, so a pair must never be split.
    Cutting at a *user* tool_result would orphan it (its tool_use summarized
    away), which is exactly what we avoid.

    Searches forward from (len - KEEP_RECENT_MESSAGES). Requires cut >= 2 so
    there is something worth summarizing. Returns None otherwise (e.g. the
    recent window is a single oversized turn that can't be compacted).
    """
    n = len(messages)
    target = max(1, n - KEEP_RECENT_MESSAGES)
    for i in range(target, n):
        if messages[i].get("role") == "assistant":
            return i if i >= 2 else None
    return None


def _summarize(
    messages,
    focus: str | None = None,
    model: str | None = None,
    system: str | None = None,
    tools=None,
) -> str | None:
    """One LLM call returning a structured summary of `messages`, or None.

    Claude Code-style compaction: rather than flattening history to text and
    sending a fresh (uncached) request, we re-send the SAME system + tools +
    history and append the summary instruction as a final user message. Because
    that prefix matches the live conversation, the call READS the existing cache
    (~0.1x) instead of reprocessing the whole history, and it summarizes the
    full-fidelity content (no per-field truncation). The cache hit holds when the
    prefix is still warm and L3 eviction hasn't just rewritten it this turn (then
    it falls back to a normal — same-sized — request). See CONTEXT_MANAGEMENT.md.

    `system`/`tools` MUST match the caller's live turn (a sub-agent passes its own
    system prompt + tool subset from its AgentDef) — otherwise the prefix
    mismatches and the call both misses the cache and summarizes under the wrong
    context. `focus` steers what to preserve (`/compact <focus>`); `model` lets a
    sub-agent's compaction run on its own model. Returns None if the model
    produced no text (e.g. it emitted only a tool_use), so the caller can refuse
    to destroy history.
    """
    from minicc import llm          # lazy: llm imports this module (avoid a cycle)
    from minicc.tools import TOOLS

    focus_line = f"\n\nFocus the summary on: {focus}" if focus else ""
    instruction = {"role": "user", "content": _COMPACT_PROMPT + focus_line}
    resp = llm.client.messages.create(
        model=model if model is not None else llm.MODEL,
        max_tokens=2048,   # 9-section summary needs a bit more room than the old 5
        system=llm._build_system_block(system),              # match the live prefix
        tools=tools if tools is not None else TOOLS,
        messages=llm._cacheable(list(messages) + [instruction]),
    )
    llm._record_usage(resp.usage)
    # tools are in scope, so the model *could* answer with a tool_use and no text.
    # Return the first text block, or None — never a fake summary (a caller that
    # replaces history with an empty summary would silently destroy context).
    return next(
        (getattr(b, "text", None) for b in resp.content
         if getattr(b, "type", None) == "text"),
        None,
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
) -> bool:
    """Summarize older messages via one LLM call; replace them in place.

    Returns True if compaction reduced the history, False if there's no safe cut
    OR the summary call produced no text (in which case the history is left
    untouched — never replaced with an empty summary). `system`/`tools` are
    threaded to _summarize so a sub-agent compacts under its own prefix.

    `trigger` ("manual" for /compact, "auto" for the budget path) is the
    PreCompact/PostCompact hook matcher + their `compact_reason` payload field
    (CC's contract). A PreCompact hook can block compaction (exit 2 or
    decision:"block"); PostCompact is notification-only, fired after success.
    """
    pre = hooks.run(
        "PreCompact", session_id=session_id, match_value=trigger, compact_reason=trigger
    )
    hooks.surface(pre)
    # `or`: for PreCompact both mean "do not compact" (unlike Stop, where
    # block and continue:false are opposites).
    if pre.block or pre.stop:
        reason = pre.reason or pre.stop_reason or "no reason given"
        ux.say(f"[compaction blocked by PreCompact hook: {reason}]", style=ux.S_INFO)
        return False

    cut = _find_cut_index(messages)
    if not cut:  # None or 0 → nothing safe to compact
        return False

    recent = messages[cut:]
    ux.say("[compacting conversation history...]", style=ux.S_INFO)
    # Summarize the FULL history (not just messages[:cut]) so the call's prefix
    # matches the live conversation and reads from cache; `recent` is still kept
    # verbatim below, so the mild overlap costs nothing structurally.
    summary = _summarize(messages, focus=focus, model=model, system=system, tools=tools)
    if not summary:
        # The model answered with no text (e.g. a tool_use). Do NOT replace the
        # history with an empty summary — leave it intact and let L5/caller decide.
        ux.say("[compaction skipped: no summary produced]", style=ux.S_ERROR)
        return False

    # recent starts with an assistant message (guaranteed by _find_cut_index),
    # so prepending just the summary as a user message keeps valid alternation:
    # user:summary -> assistant -> user -> ...  No dummy assistant needed.
    messages[:] = [
        {"role": "user", "content": f"[Earlier conversation summary]\n\n{summary}"},
    ] + recent
    ctx.compactions += 1
    # Record the boundary to the transcript so a resume reconstructs [summary]+tail
    # instead of re-inflating the raw log (main session only; sub-agents pass None).
    if session_id:
        sessions.log_compaction(session_id, messages)
    ux.say(f"[compacted {cut} messages into a summary]", style=ux.S_INFO)
    post = hooks.run(
        "PostCompact", session_id=session_id, match_value=trigger, compact_reason=trigger
    )
    hooks.surface(post)
    return True


def recap(messages, focus: str | None = None) -> str:
    """Summarize the conversation WITHOUT mutating it (for /recap).

    Cache-safe: it doesn't touch `messages`, so the conversation prefix and its
    cache stay intact (unlike /compact, which replaces history).
    """
    if len(messages) < 2:
        return "(nothing to recap yet)"
    return _summarize(messages, focus=focus) or "(no summary produced)"


def context_usage(ctx: ContextState, messages, model: str | None = None) -> dict:
    """Structured data about current context usage (for /context).

    `estimated_tokens` is the real input size of the last request
    (response.usage), or a char-estimate before the first response. `budget` is
    the compaction trigger: the model window minus COMPACT_BUFFER_TOKENS
    (CC's `effectiveContextWindow - 13K`; see effective_budget).
    """
    if model is None:
        from minicc import llm      # lazy (cycle); the session's current model
        model = llm.MODEL
    tokens = ctx.context_size(messages)
    budget = effective_budget(model)
    pct = (tokens / budget * 100) if budget else 0

    tool_results = 0
    evicted = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tool_results += 1
                if b.get("content") == EVICTED_MARKER:
                    evicted += 1

    return {
        "estimated_tokens": tokens,
        "budget": budget,
        "pct_of_budget": pct,
        "messages": len(messages),
        "tool_results": tool_results,
        "evicted": evicted,
        "eviction_events": ctx.evictions,
        "compaction_events": ctx.compactions,
    }
