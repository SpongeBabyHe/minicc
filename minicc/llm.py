import os
from dotenv import load_dotenv
from anthropic import Anthropic, APIStatusError
from minicc.tools import TOOLS
from minicc.prompts.system import build_system_prompt
from minicc import compact, config, ux

load_dotenv()  # ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL only; model lives in config
MODEL = config.resolve_model()
# max_retries: the SDK retries transient failures (429/500/503/connection) with
# exponential backoff + jitter, honoring Retry-After. Bumped from the default 2
# to ride out brief rate-limit spikes during dogfood. (Structurally-too-big
# requests are handled by L3/L4, not retries.)
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"), max_retries=4)
SYSTEM = build_system_prompt()
# TTL for the STABLE prefix layers (system+tools / project / session): "5m" or
# "1h" (settings `cache_ttl`; GA, no beta header). 1h writes once at 2x, then
# every hit refreshes free — insurance for >5-min gaps between turns. The rolling
# conversation breakpoint stays at the default 5m: the API requires longer-TTL
# breakpoints to precede shorter ones, and the stable layers render first.
CACHE_TTL = config.resolve_cache_ttl()


def _prefix_cache_control() -> dict:
    """cache_control for the stable prefix blocks (a fresh dict each call)."""
    if CACHE_TTL == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def get_model() -> str:
    """The model id used for inference — single source of truth (see /model)."""
    return MODEL


def set_model(model_id: str) -> None:
    """Switch the session's model in place (see /model; not persisted)."""
    global MODEL
    MODEL = model_id


# Context management (budgets / eviction / compaction / ContextState) lives in
# compact.py — CC keeps it in services/compact/, a peer of the API client, and
# so does minicc (docs/CC_CONTEXT_MANAGEMENT.md §7). This module is the client:
# request assembly, cache layering, streaming, usage accounting.

_USAGE = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "web_searches": 0}
_SESSION_CONTEXT = ""


def set_session_context(text: str):
    """Update session context (cache layer 2: env + git snapshot, volatile-last).
    Called on startup and /clear."""
    global _SESSION_CONTEXT
    _SESSION_CONTEXT = text


def _build_system_block(system: str | None = None) -> list:
    """Build the `system` param as content blocks with cache_control markers.

    Cache prefix layers (each cache_control = one breakpoint), static→dynamic like
    Claude Code:
      1. System prompt — rarely changes. Its breakpoint's prefix is `tools +
         system` (tools render first), so this single marker caches the tool
         definitions too — no separate tools breakpoint needed (see tools/__init__).
      2. Session context — env + git snapshot, VOLATILE-LAST so a change here (only
         on /clear) never busts layer 1. Mirrors CC, whose system prompt also
         carries env + gitStatus.
    Plus the conversation breakpoint (_cacheable) = 3 of the 4 allowed, one spare.

    CLAUDE.md, the auto-memory index, and the skill listing are NOT prefix
    layers: CC injects them as <system-reminder> messages in the conversation,
    and so does minicc (reminders.py) — volatile content on the append side
    never busts the prefix cache.

    A sub-agent passes its own `system` string → a single isolated block (no
    session layer; its context is deliberately its own).
    """
    if system:
        return [
            {"type": "text", "text": system, "cache_control": _prefix_cache_control()}
        ]

    blocks = [
        {"type": "text", "text": SYSTEM, "cache_control": _prefix_cache_control()}
    ]
    if _SESSION_CONTEXT:
        blocks.append(
            {
                "type": "text",
                "text": _SESSION_CONTEXT,
                "cache_control": _prefix_cache_control(),
            }
        )
    return blocks


def _record_usage(usage) -> tuple[int, int]:
    """Accumulate one response's token usage into the session counters (/cost).
    Returns (cache_read, cache_creation) — llm_response also needs them to
    compute the next turn's real input size."""
    cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_c = getattr(usage, "cache_creation_input_tokens", 0) or 0
    _USAGE["input"] += usage.input_tokens
    _USAGE["output"] += usage.output_tokens
    _USAGE["cache_read"] += cache_r
    _USAGE["cache_creation"] += cache_c
    return cache_r, cache_c


def _thinking_param(model: str) -> dict | None:
    """Adaptive thinking for models that support it (4.6+ / Opus 4.7+ / Fable).

    CC runs with thinking ON — the three-arm /init transcripts show thinking
    blocks in both CC arms while minicc's Sonnet ran bare, an uncontrolled
    variable in the harness comparison and a fidelity gap in its own right.
    Adaptive is the only on-mode on 4.7+ (budget_tokens is a 400 there); models
    predating adaptive (Haiku 4.5 sub-agents) get no thinking param at all.
    Thinking blocks come back in `response.content` and minicc already appends
    content verbatim to history + transcript, which is exactly the replay
    contract (pass thinking blocks back unchanged on the same model)."""
    known_adaptive = ("sonnet-4-6", "opus-4-6", "opus-4-7", "opus-4-8", "fable")
    if any(k in model for k in known_adaptive):
        return {"type": "adaptive"}
    return None


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


def _send_request(params: dict, stream: bool):
    """Issue one Messages request and return the final message. Streaming shows a
    spinner + text deltas; both paths return the same shape create() would, so
    downstream tool-dispatch/usage logic is identical. Kept separate so the
    reactive-413 path can retry it without duplicating the stream plumbing."""
    if not stream:
        return client.messages.create(**params)   # tests, scripts, sub-agents
    with ux.streaming() as render:
        with client.messages.stream(**params) as s:
            for delta in s.text_stream:
                render(delta)
            return s.get_final_message()


def llm_response(
    messages,
    system: str | None = None,
    stream: bool = True,
    tools=None,
    model: str | None = None,
    session_id: str | None = None,
    ctx: "compact.ContextState | None" = None,
):
    m = (
        model if model is not None else MODEL
    )  # per-call override (sub-agents); else global MODEL
    # ctx: THIS conversation's trigger state. A loop (agent_loop) passes one per
    # conversation; a bare single-shot call gets a fresh throwaway — so a nested
    # call (sub-agent, web_fetch extraction, /memory consolidate) can never
    # poison another conversation's trigger, in either direction.
    ctx = ctx if ctx is not None else compact.ContextState()
    size = ctx.context_size(messages)           # real (last usage) or cold estimate
    budget = compact.effective_budget(m)
    if size > budget:
        # Over the compaction budget → L4 compaction (the bigger reset). We do NOT
        # also evict this turn: compaction replaces the old messages anyway, and
        # skipping eviction keeps the summary call on a warm cache.
        if ctx.compact_attempts >= compact.MAX_COMPACT_ATTEMPTS:  # L5 thrash guard
            ux.say(
                "[Autocompact is thrashing: still over budget after "
                f"{compact.MAX_COMPACT_ATTEMPTS} compactions. A single message is "
                "likely too large. Try /clear, or read large files in smaller "
                "chunks (offset/limit).]",
                style=ux.S_ERROR,
            )
            raise RuntimeError("compact thrashing")
        ctx.compact_attempts += 1
        # Thread system/tools so a sub-agent compacts under ITS prefix; session_id
        # so the main session records the compaction boundary to its transcript.
        compact.compact(ctx, messages, model=m, system=system, tools=tools, session_id=session_id)
    else:
        ctx.compact_attempts = 0
        # L3: CC-style incremental tool_result eviction between CLEAR_TRIGGER and
        # the compaction budget — cheap (no LLM call), guarded by clear_at_least so
        # it only breaks the cache when it frees ≥ CLEAR_AT_LEAST tokens.
        if size > compact.CLEAR_TRIGGER:
            evicted = compact.evict_old_tool_results(messages, min_free=compact.CLEAR_AT_LEAST)
            if evicted:
                ctx.evictions += 1
                ux.say(
                    f"[evicted {evicted} old tool_results to reclaim context]",
                    style=ux.S_INFO,
                )

    params = dict(
        model=m,
        messages=_cacheable(messages),  # L1: cache the conversation history too
        # Thinking tokens share this budget — 8000 starved long turns once
        # adaptive thinking landed, so the ceiling doubles (well under Sonnet's
        # 64K output cap; max_tokens is a safety ceiling, not a target).
        max_tokens=16_000,
        system=_build_system_block(system),
        tools=tools if tools is not None else TOOLS,
    )
    thinking = _thinking_param(m)
    if thinking:
        params["thinking"] = thinking
    try:
        response = _send_request(params, stream)
    except APIStatusError as e:
        # Reactive compaction (CC-style fallback).
        #  413: request too large for the window (proactive trigger under-fired, or
        #       a single turn overflowed) → must shrink. SDK does NOT auto-retry 413.
        #  429: rate limited. The SDK already retried with backoff; if it still
        #       persists AND the context is large, a single request likely exceeds
        #       the per-minute input budget (the PAIN.md case), which only shrinking
        #       fixes. A small-context 429 is a transient/quota limit compaction
        #       can't help, so it surfaces (don't destroy history over a temporary cap).
        code = getattr(e, "status_code", None)
        if code not in (413, 429):
            raise
        if code == 429 and compact.estimate_tokens(messages) <= compact.CLEAR_TRIGGER:
            raise
        ux.say("[request rejected — compacting and retrying]", style=ux.S_ERROR)
        if not compact.compact(ctx, messages, model=m, system=system, tools=tools, session_id=session_id):
            raise  # nothing compactable → let the error surface
        params["messages"] = _cacheable(messages)
        response = _send_request(params, stream)

    cache_r, cache_c = _record_usage(response.usage)
    # Server-tool usage (web_search): billed per search ($10/1k), shown in /cost.
    stu = getattr(response.usage, "server_tool_use", None)
    if stu:
        _USAGE["web_searches"] += getattr(stu, "web_search_requests", 0) or 0
    # Real total input of THIS request → this conversation's next trigger read.
    ctx.last_input_tokens = response.usage.input_tokens + cache_r + cache_c
    return response


def get_usage() -> dict:
    """Cumulative token usage since process start."""
    return dict(_USAGE)
