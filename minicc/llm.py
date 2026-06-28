import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic
from pathlib import Path
from minicc.tools import TOOLS
from minicc.prompts.system import build_system_prompt, load_project_context
from minicc import ux
from minicc import config

load_dotenv()  # ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL only; model lives in config
MODEL = config.resolve_model()
# max_retries: the SDK retries transient failures (429/500/503/connection) with
# exponential backoff + jitter, honoring Retry-After. Bumped from the default 2
# to ride out brief rate-limit spikes during dogfood. (Structurally-too-big
# requests are handled by L3/L4, not retries.)
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"), max_retries=4)
SYSTEM = build_system_prompt()


def get_model() -> str:
    """The model id used for inference — single source of truth (see /model)."""
    return MODEL


def set_model(model_id: str) -> None:
    """Switch the model for in-session."""
    global MODEL
    MODEL = model_id


# ─── L3: Token budget for tool_result eviction ──────────────────────────────
# Above this estimated token count, llm_response() evicts old tool_result
# contents to keep the request from blowing up. The number is minicc's own
# choice (CC docs don't publish theirs) and may be tuned via dogfood.
# set to 2_000 for test triggering
TOKEN_BUDGET = 150_000

# Number of most-recent tool_result blocks to keep intact when evicting.
RECENT_TOOL_RESULTS_KEEP = 4

# Placeholder content for an evicted tool_result. The model can read this
# and decide to re-invoke the tool if it actually needs the data.
EVICTED_MARKER = (
    "[content omitted; was earlier in conversation — re-call the tool if needed]"
)


_USAGE = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
_PROJECT_CONTEXT = ""

# Durable counters for context-management activity this session. Surfaced via
# /context so you can tell whether L3/L4 fired without hunting for dim log
# lines that scroll past.
_CTX_STATS = {"evictions": 0, "compactions": 0}


# ─── L4: LLM compaction when eviction (L3) isn't enough ─────────────────────
# How many recent messages to keep verbatim after compaction. The actual cut
# lands on an assistant-message boundary at or after this point (see
# _find_cut_index), keeping tool_use/tool_result pairs intact.
KEEP_RECENT_MESSAGES = 6

# ─── L5: thrashing guard ────────────────────────────────────────────────────
# If we're still over budget after this many compactions in a row, a single
# message is too large to compact away — stop and error instead of looping.
MAX_COMPACT_ATTEMPTS = 3
_compact_attempts = 0

_COMPACT_PROMPT = """You compress an agent's conversation history into a structured summary so work can continue with less context. Output this exact structure:

## Goal
<the user's overall objective, 1-2 sentences>

## Done
- <action taken + files touched + outcome>

## Key findings
- <important facts, decisions, discoveries worth keeping>

## In progress
<what's being worked on now and the immediate next step>

## Open questions
- <anything unresolved>

Be specific (file paths, decisions). Under 500 words. No pleasantries."""


def set_project_context(text: str):
    """Update project context (cache layer 2). Called on startup and /clear."""
    global _PROJECT_CONTEXT
    _PROJECT_CONTEXT = text


def _build_system_block(system: str | None = None) -> list:
    """Build the `system` param as content blocks with cache_control markers.

    Cache prefix layers (each cache_control = one breakpoint), CC-style grouping:
      1. System prompt — rarely changes. Its breakpoint's prefix is `tools +
         system` (tools render first), so this single marker caches the tool
         definitions too — no separate tools breakpoint needed (see tools/__init__).
      2. Project context — CLAUDE.md, changes on /clear.
    That leaves budget (max 4/request) for the conversation breakpoint (_cacheable).
    """
    if system:
        return [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

    blocks = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    if _PROJECT_CONTEXT:
        blocks.append(
            {
                "type": "text",
                "text": _PROJECT_CONTEXT,
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks


def _estimate_tokens(messages) -> int:
    """Rough token estimate, ~4 chars per token.

    Uses JSON serialization to handle both dict-form messages and the
    Anthropic SDK's Block objects. Overestimates slightly (JSON overhead +
    repr of objects), which is fine for trigger decisions — evicting a turn
    earlier than strictly needed is better than blowing the budget.
    """
    try:
        return len(json.dumps(messages, default=str)) // 4
    except Exception as e:
        return 0


def _evict_old_tool_result(messages) -> int:
    """Replace `content` of old tool_result blocks with EVICTED_MARKER.

    Keeps the RECENT_TOOL_RESULTS_KEEP most recent intact. Returns the count
    of blocks evicted. Mutates `messages` in place.

    Conversation structure (assistant tool_use → user tool_result) is
    preserved. The model still sees that the tool was called; only the
    content is gone, and it can re-call if needed.
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


def _summarize(messages, focus: str | None = None, model: str | None = None) -> str:
    """One LLM call returning a structured summary of `messages`.

    Claude Code-style compaction: rather than flattening history to text and
    sending a fresh (uncached) request, we re-send the SAME system + tools +
    history and append the summary instruction as a final user message. Because
    that prefix matches the live conversation, the call READS the existing cache
    (~0.1x) instead of reprocessing the whole history, and it summarizes the
    full-fidelity content (no per-field truncation). The cache hit holds when the
    prefix is still warm and L3 eviction hasn't just rewritten it this turn (then
    it falls back to a normal — same-sized — request). See CONTEXT_MANAGEMENT.md.

    `focus` steers what to preserve (`/compact <focus>`). `model` lets a
    sub-agent's compaction run on its own model; defaults to the global MODEL.
    """
    focus_line = f"\n\nFocus the summary on: {focus}" if focus else ""
    instruction = {"role": "user", "content": _COMPACT_PROMPT + focus_line}
    resp = client.messages.create(
        model=model if model is not None else MODEL,
        max_tokens=1500,
        system=_build_system_block(),       # same prefix as the live turn -> cache read
        tools=TOOLS,
        messages=_cacheable(list(messages) + [instruction]),
    )
    _USAGE["input"] += resp.usage.input_tokens
    _USAGE["output"] += resp.usage.output_tokens
    _USAGE["cache_read"] += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    _USAGE["cache_creation"] += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
    # The instruction is explicit, but tools are in scope, so guard against a
    # leading non-text block by taking the first text block.
    text = next(
        (getattr(b, "text", None) for b in resp.content
         if getattr(b, "type", None) == "text"),
        None,
    )
    return text or "(empty summary)"


def _compact(messages, focus: str | None = None, model: str | None = None) -> bool:
    """Summarize older messages via one LLM call; replace them in place.

    Returns True if compaction reduced the history, False if no safe cut.
    """
    cut = _find_cut_index(messages)
    if not cut:  # None or 0 → nothing safe to compact
        return False

    recent = messages[cut:]
    ux.say("[compacting conversation history...]", style=ux.S_INFO)
    # Summarize the FULL history (not just messages[:cut]) so the call's prefix
    # matches the live conversation and reads from cache; `recent` is still kept
    # verbatim below, so the mild overlap costs nothing structurally.
    summary = _summarize(messages, focus=focus, model=model)

    # recent starts with an assistant message (guaranteed by _find_cut_index),
    # so prepending just the summary as a user message keeps valid alternation:
    # user:summary -> assistant -> user -> ...  No dummy assistant needed.
    messages[:] = [
        {"role": "user", "content": f"[Earlier conversation summary]\n\n{summary}"},
    ] + recent
    _CTX_STATS["compactions"] += 1
    ux.say(f"[compacted {cut} messages into a summary]", style=ux.S_INFO)
    return True


def compact(messages, focus: str | None = None) -> bool:
    """Manual compaction entry point (for /compact). Returns True if it ran."""
    return _compact(messages, focus=focus)


def recap(messages, focus: str | None = None) -> str:
    """Summarize the conversation WITHOUT mutating it (for /recap).

    Cache-safe: it doesn't touch `messages`, so the conversation prefix and its
    cache stay intact (unlike /compact, which replaces history).
    """
    if len(messages) < 2:
        return "(nothing to recap yet)"
    return _summarize(messages, focus=focus)


def _cacheable(messages):
    """Request-time copy of `messages` with a `cache_control` breakpoint on the
    last block of the last message — the API then reads the prior conversation
    from cache (~0.1x input) instead of re-paying full price each turn.

    String user content is normalized to a text block so a message's bytes are
    identical whether it's the last turn or sunk into mid-history; otherwise the
    cached prefix wouldn't match across turns. Does NOT mutate the stored history
    (eviction L3 + serialization keep the clean form). See CONTEXT_MANAGEMENT.md
    § Token economy. (cache_control is a marker, not content, so the breakpoint
    moving forward each turn doesn't invalidate earlier cache writes.)
    """
    # not deepcopy, only normalize string content to a text block, keep the rest of the message as is and as where it is.
    out = [
        (
            {"role": m["role"], "content": [{"type": "text", "text": m["content"]}]}
            if isinstance(m.get("content"), str)
            else m
        )
        for m in messages
    ]
    if out:
        last = out[-1]
        c = last["content"]
        if isinstance(c, list) and c and isinstance(c[-1], dict):
            out[-1] = {
                **last,
                "content": c[:-1] + [{**c[-1], "cache_control": {"type": "ephemeral"}}],
            }
    return out


def llm_response(
    messages,
    system: str | None = None,
    stream: bool = True,
    tools=None,
    model: str | None = None,
):
    m = (
        model if model is not None else MODEL
    )  # per-call override (sub-agents); else global MODEL
    global _compact_attempts
    if _estimate_tokens(messages) <= TOKEN_BUDGET:
        _compact_attempts = 0
    else:
        # L3: evict old tool_result blocks to keep the request from blowing up.
        evicted = _evict_old_tool_result(messages)
        if evicted:
            _CTX_STATS["evictions"] += 1
            ux.say(
                f"[evicted {evicted} old tool_results to reduce context]",
                style=ux.S_INFO,
            )
        # L4: LLM compaction when eviction (L3) isn't enough.
        if _estimate_tokens(messages) > TOKEN_BUDGET:
            # L5: thrashing guard.
            if _compact_attempts >= MAX_COMPACT_ATTEMPTS:
                ux.say(
                    "[Autocompact is thrashing: still over budget after "
                    f"{MAX_COMPACT_ATTEMPTS} compactions. A single message is "
                    "likely too large. Try /clear, or read large files in "
                    "smaller chunks (offset/limit).]",
                    style=ux.S_ERROR,
                )
                raise RuntimeError("compact thrashing")
            _compact_attempts += 1
            _compact(messages, model=m)

    params = dict(
        model=m,
        messages=_cacheable(messages),  # L1: cache the conversation history too
        max_tokens=8000,
        system=_build_system_block(system),
        tools=tools if tools is not None else TOOLS,
    )
    if not stream:
        # non-streaming path: tests, scripts, subagents
        response = client.messages.create(**params)
    else:
        # streaming path: spinner until first token, then print text deltas.
        # get_final_message() returns the SAME shape create() would — so all
        # downstream logic (tool dispatch, usage) is unchanged.
        with ux.streaming() as render:
            with client.messages.stream(**params) as s:
                for delta in s.text_stream:
                    render(delta)
                response = s.get_final_message()

    _USAGE["input"] += response.usage.input_tokens
    _USAGE["output"] += response.usage.output_tokens
    _USAGE["cache_read"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0
    _USAGE["cache_creation"] += (
        getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    )
    return response


def get_usage() -> dict:
    """Cumulative token usage since process start."""
    return dict(_USAGE)


def context_usage(messages) -> dict:
    """Structured data about current context usage (for /context).

    Note: estimated_tokens covers the conversation history ONLY — it does
    not include the system prompt or tool definitions that also go into each
    request. This is the number L3 eviction watches.
    """
    tokens = _estimate_tokens(messages)
    pct = (tokens / TOKEN_BUDGET * 100) if TOKEN_BUDGET else 0

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
        "budget": TOKEN_BUDGET,
        "pct_of_budget": pct,
        "messages": len(messages),
        "tool_results": tool_results,
        "evicted": evicted,
        "eviction_events": _CTX_STATS["evictions"],
        "compaction_events": _CTX_STATS["compactions"],
    }
