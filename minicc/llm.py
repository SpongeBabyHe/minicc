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
# set to 2000 for test triggering
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


def llm_response(messages, system: str | None = None):
    # If the messages are too long, evict old tool_result blocks to keep the request from blowing up.
    if _estimate_tokens(messages) > TOKEN_BUDGET:
        evicted = _evict_old_tool_result(messages)
        if evicted:
            ux.say(
                f"[evicted {evicted} old tool_results to reduce context]",
                style=ux.S_INFO,
            )
    response = client.messages.create(
        model=MODEL,
        messages=messages,
        max_tokens=8000,
        system=_build_system_block(system),
        tools=TOOLS,
    )
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
    }
