"""This module is the client: request assembly, cache layering, streaming, usage accounting."""

import os
from dotenv import load_dotenv
from anthropic import Anthropic, APIStatusError
from minicc.tools import TOOLS
from minicc.prompts.system import build_system_prompt
from minicc import config, context_management, ux

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


_USAGE = {"input": 0, "output": 0, "cache_read": 0,
          "cache_creation": 0, "web_searches": 0}
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
         definitions too — no separate tools breakpoint needed.
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
    known_adaptive = ("sonnet-4-6", "opus-4-6",
                      "opus-4-7", "opus-4-8", "fable")
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
            {"role": m["role"], "content": [
                {"type": "text", "text": m["content"]}]}
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


def summary_runtime() -> context_management.SummaryRuntime:
    """Expose only the request capabilities needed by context summarization."""
    return context_management.SummaryRuntime(
        default_model=MODEL,
        default_tools=TOOLS,
        build_system_block=_build_system_block,
        cacheable=_cacheable,
        thinking_param=_thinking_param,
        create_message=client.messages.create,
        record_usage=_record_usage,
    )


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
    ctx: "context_management.ContextState | None" = None,
):
    m = (
        model if model is not None else MODEL
    )  # per-call override (sub-agents); else global MODEL
    # ctx: THIS conversation's trigger state. A loop (agent_loop) passes one per
    # conversation; a bare single-shot call gets a fresh throwaway — so a nested
    # call (sub-agent, web_fetch extraction, /memory consolidate) can never
    # poison another conversation's trigger, in either direction.
    ctx = ctx if ctx is not None else context_management.ContextState()
    # real (last usage) or cold estimate
    size = ctx.context_size(messages)
    budget = context_management.effective_budget(m)
    if size > budget:
        # Over the compaction budget → L4 compaction (the bigger reset). We do NOT
        # also evict this turn: compaction replaces the old messages anyway, and
        # skipping eviction keeps the summary call on a warm cache.
        if ctx.compact_attempts >= context_management.MAX_COMPACT_ATTEMPTS:
            ux.say(
                "[Autocompact is thrashing: still over budget after "
                f"{context_management.MAX_COMPACT_ATTEMPTS} compactions. "
                "A single message is "
                "likely too large. Try /clear, or read large files in smaller "
                "chunks (offset/limit).]",
                style=ux.S_ERROR,
            )
            raise RuntimeError("compact thrashing")
        # Thread system/tools so a sub-agent compacts under ITS prefix; session_id
        # so the main session records the compaction boundary to its transcript.
        result = context_management.compact(
            ctx,
            messages,
            model=m,
            system=system,
            tools=tools,
            session_id=session_id,
            runtime=summary_runtime(),
        )
        # Count toward the thrash guard only when compaction was ATTEMPTED — a
        # PreCompact hook veto (result is None) is the user's choice: proceed
        # uncompacted without counting, so a standing veto never crashes.
        if result is not None:
            ctx.compact_attempts += 1
    else:
        ctx.compact_attempts = 0
        # L3: local fallback between the 100K trigger and compaction budget.
        # Only re-fetchable tools are eligible, five recent results stay intact,
        # and the cache is broken only for an estimated ≥20K net saving.
        if size > context_management.TOOL_RESULT_EVICTION_TRIGGER_TOKENS:
            eviction_kwargs = {
                "min_savings_tokens": (
                    context_management.TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS
                )
            }
            if session_id:
                eviction_kwargs["session_id"] = session_id
            evicted = context_management.evict_old_tool_results(
                messages,
                **eviction_kwargs,
            )
            if evicted:
                ctx.record_eviction(evicted)
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

    params = dict(
        model=m,
        # L1: cache the conversation history too
        messages=_cacheable(messages),
        # Thinking tokens share this budget — 8000 starved long turns once
        # adaptive thinking landed, so the ceiling doubles (well under Sonnet's
        # 64K output cap; max_tokens is a safety ceiling, not a target).
        max_tokens=context_management.MAX_OUTPUT_TOKENS,
        system=_build_system_block(system),
        tools=tools if tools is not None else TOOLS,
    )
    thinking = _thinking_param(m)
    if thinking:
        params["thinking"] = thinking
    sent_message_tokens = context_management.estimate_tokens(messages)
    try:
        response = _send_request(params, stream)
    except APIStatusError as e:
        # Reactive compaction for structural request-size failures only.
        #  400 "prompt is too long": input alone exceeds the context window — the
        #       token-overflow case. 400 is broad, so match the message.
        #  413: request exceeds the 32MB request-BYTE limit (Cloudflare, before the
        #       API). A bounded prefix summary can recover without resending the
        #       same oversized body. 429 is deliberately excluded: it can mean RPM,
        #       token, acceleration, or workspace limits, so deleting history is not
        #       a justified response after the SDK's normal retries.
        code = getattr(e, "status_code", None)
        msg = " ".join(
            str(part)
            for part in (
                getattr(e, "message", ""),
                getattr(e, "body", ""),
            )
            if part
        ).lower()
        token_overflow_400 = code == 400 and "too long" in msg
        if code != 413 and not token_overflow_400:
            raise
        ux.say("[request rejected — shrinking context and retrying]", style=ux.S_ERROR)
        recovery = "bytes" if code == 413 else "tokens"
        # First discard oversized tool outputs locally, including recent ones:
        # unlike normal L3 eviction, a rejected request cannot afford to protect
        # five blocks that may themselves be the cause.
        eviction_kwargs = {
            "min_savings_tokens": 0,
            "keep_recent": 0,
            "allow_lossy_fallback": True,
        }
        if session_id:
            eviction_kwargs["session_id"] = session_id
        evicted = context_management.evict_old_tool_results(
            messages,
            **eviction_kwargs,
        )
        if evicted:
            ctx.record_eviction(evicted)
            ctx.rebase(messages)

        # A single legal summary request can only consume one model window. Very
        # large byte/token failures may therefore need several bounded chunks.
        # Never issue the live retry while local bounds still say it will fail.
        must_compact = not evicted
        recovery_compactions = 0
        while must_compact or context_management.recovery_needed(
            messages,
            m,
            recovery,
        ):
            if (
                recovery_compactions
                >= context_management.MAX_RECOVERY_COMPACTIONS
            ):
                raise
            result = context_management.compact(
                ctx,
                messages,
                model=m,
                system=system,
                tools=tools,
                session_id=session_id,
                recovery=recovery,
                runtime=summary_runtime(),
            )
            if not result:
                raise  # nothing compactable → let the original error surface
            recovery_compactions += 1
            must_compact = False
        params["messages"] = _cacheable(messages)
        sent_message_tokens = context_management.estimate_tokens(messages)
        response = _send_request(params, stream)

    cache_r, cache_c = _record_usage(response.usage)
    # Server-tool usage (web_search): billed per search ($10/1k), shown in /cost.
    stu = getattr(response.usage, "server_tool_use", None)
    if stu:
        _USAGE["web_searches"] += getattr(stu, "web_search_requests", 0) or 0
    # Anchor real total input to the exact stored messages that produced it. The
    # next trigger adds any estimated message growth instead of using stale usage
    # unchanged for a newly appended user/tool message.
    ctx.record_input(
        response.usage.input_tokens + cache_r + cache_c,
        sent_message_tokens,
    )
    return response


def get_usage() -> dict:
    """Cumulative token usage since process start."""
    return dict(_USAGE)
