import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic
from pathlib import Path
from minicc.tools import TOOLS
from minicc.prompts.system import build_system_prompt, load_project_context
from minicc import ux

load_dotenv()

MODEL = os.environ["MODEL_ID"]
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
SYSTEM = build_system_prompt()

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

# Per-field cap when flattening history for the summarization call, so the
# summary request itself can't balloon (e.g. a big write_file content).
SUMMARY_FIELD_CAP = 1000

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

    Three cache prefix layers (each cache_control = one breakpoint):
      1. System prompt   — rarely changes
      2. Project context — CLAUDE.md, changes on /clear
      (3. Tools live in the tools= param; last tool carries its own marker)

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


def _serialize_for_summary(messages) -> str:
    """Flatten messages to plain text for the summarization call.

    Handles both dict-form messages and SDK Block objects. EVERY field is
    capped at SUMMARY_FIELD_CAP so the summary call's own input stays bounded —
    without this, a large write_file content (in a tool_use input) or a long
    answer would make the summarization request itself huge, risking the very
    rate limit we are compacting to avoid.
    """
    CAP = SUMMARY_FIELD_CAP
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str):  # user query
            parts.append(f"[{role}] {content[:CAP]}")
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):  # tool result appears as a dict
                    if b.get("type") == "tool_result":
                        parts.append(
                            f"[{role} tool_result] {str(b.get('content'))[:CAP]}"
                        )
                    elif b.get("type") == "text":
                        parts.append(f"[{role} text] {str(b.get('text', ''))[:CAP]}")
                else:  # response from LLM is SDK object [ToolUseBlock, TextBlock]
                    bt = getattr(b, "type", None)
                    if bt == "text":
                        parts.append(
                            f"[{role} text] {str(getattr(b, 'text', ''))[:CAP]}"
                        )
                    elif bt == "tool_use":
                        parts.append(
                            f"[{role} tool_use] {getattr(b, 'name', '')}({str(getattr(b, 'input', {}))[:CAP]})"
                        )
    return "\n".join(parts)


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


def _summarize(messages, focus: str | None = None) -> str:
    """One LLM call returning a structured summary of `messages`.

    Shared by _compact (L4) and recap (L6c). `focus`, if given, steers what the
    summary preserves (powers `/compact <focus>`).
    """
    focus_line = f"\n\nFocus the summary on: {focus}" if focus else ""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=_COMPACT_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Summarize this agent history:{focus_line}\n\n{_serialize_for_summary(messages)}",
            }
        ],
    )
    _USAGE["input"] += resp.usage.input_tokens
    _USAGE["output"] += resp.usage.output_tokens
    return resp.content[0].text if resp.content else "(empty summary)"


def _compact(messages, focus: str | None = None) -> bool:
    """Summarize older messages via one LLM call; replace them in place.

    Returns True if compaction reduced the history, False if no safe cut.
    """
    cut = _find_cut_index(messages)
    if not cut:  # None or 0 → nothing safe to compact
        return False

    older, recent = messages[:cut], messages[cut:]
    ux.say("[compacting conversation history...]", style=ux.S_INFO)
    summary = _summarize(older, focus=focus)

    # recent starts with an assistant message (guaranteed by _find_cut_index),
    # so prepending just the summary as a user message keeps valid alternation:
    # user:summary -> assistant -> user -> ...  No dummy assistant needed.
    messages[:] = [
        {"role": "user", "content": f"[Earlier conversation summary]\n\n{summary}"},
    ] + recent
    _CTX_STATS["compactions"] += 1
    ux.say(f"[compacted {len(older)} messages into a summary]", style=ux.S_INFO)
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


def llm_response(messages, system: str | None = None, stream: bool = True, tools=None):
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
            _compact(messages)

    params = dict(
        model=MODEL,
        messages=messages,
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
