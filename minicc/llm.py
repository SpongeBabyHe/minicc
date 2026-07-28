"""Anthropic Messages API boundary for live agent turns.

This module owns process-level model configuration, request assembly, transport,
API-error classification, and usage accounting. Context policy lives in
``minicc.context_management.manager``; :func:`llm_response` preserves the
required prepare → assemble → send → recover → account sequence.
"""

import os

from anthropic import Anthropic, APIStatusError
from dotenv import load_dotenv

from minicc import config, context_management, ux
from minicc.prompts.system import build_system_prompt
from minicc.tools import TOOLS

load_dotenv()
MODEL = config.resolve_model()
CACHE_TTL = config.resolve_cache_ttl()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"), max_retries=4)
SYSTEM = build_system_prompt()
_USAGE = {
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_creation": 0,
    "web_searches": 0,
}
_SESSION_CONTEXT = ""


def get_model() -> str:
    """Return the process-wide default model for subsequent requests."""
    return MODEL


def set_model(model_id: str) -> None:
    """Set the process-wide default model without persisting the selection."""
    global MODEL
    MODEL = model_id


def set_session_context(text: str) -> None:
    """Replace the volatile environment and Git snapshot used by main requests."""
    global _SESSION_CONTEXT
    _SESSION_CONTEXT = text


def get_usage() -> dict:
    """Return a snapshot of cumulative process usage counters."""
    return dict(_USAGE)


def _prefix_cache_control() -> dict:
    """Return a fresh cache-control marker for a stable prefix block."""
    if CACHE_TTL == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def _build_system_block(system: str | None = None) -> list:
    """Build cache-aware system blocks for a main or isolated request."""
    if system:
        return [
            {
                "type": "text",
                "text": system,
                "cache_control": _prefix_cache_control(),
            }
        ]

    blocks = [
        {
            "type": "text",
            "text": SYSTEM,
            "cache_control": _prefix_cache_control(),
        }
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


def _thinking_param(model: str) -> dict | None:
    """Return explicit adaptive-thinking configuration for known model families.

    Older or unknown models omit the parameter rather than risking an invalid
    request. Live and summary requests share this helper so their request shape
    and thinking-block replay contract remain aligned.
    """
    known_adaptive = (
        "sonnet-4-6",
        "sonnet-5",
        "opus-4-6",
        "opus-4-7",
        "opus-4-8",
        "opus-5",
        "fable",
        "mythos",
    )
    if any(k in model for k in known_adaptive):
        return {"type": "adaptive"}
    return None


def _cacheable(messages):
    """Return a request-time message view with a final cache breakpoint."""
    out = [
        (
            {
                "role": message["role"],
                "content": [{"type": "text", "text": message["content"]}],
            }
            if isinstance(message.get("content"), str)
            else message
        )
        for message in messages
    ]
    if out:
        last = out[-1]
        content = last["content"]
        if (
            isinstance(content, list)
            and content
            and isinstance(content[-1], dict)
        ):
            out[-1] = {
                **last,
                "content": content[:-1]
                + [
                    {
                        **content[-1],
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
    return out


def _build_request_params(
    messages,
    *,
    model: str,
    system: str | None,
    tools,
) -> dict:
    """Build the exact live request used by sizing, transport, and recovery."""
    params = {
        "model": model,
        "messages": _cacheable(messages),
        "max_tokens": context_management.resolve_max_output_tokens(),
        "system": _build_system_block(system),
        "tools": tools if tools is not None else TOOLS,
    }
    thinking = _thinking_param(model)
    if thinking:
        params["thinking"] = thinking
    return params


def _send_request(params: dict, stream: bool):
    """Send one request and normalize streaming to a final Message object."""
    if not stream:
        return client.messages.create(**params)
    with ux.streaming() as render:
        with client.messages.stream(**params) as stream_response:
            for delta in stream_response.text_stream:
                render(delta)
            return stream_response.get_final_message()


def _record_usage(usage) -> tuple[int, int]:
    """Accumulate response usage and return its cache-read and cache-write tokens."""
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    _USAGE["input"] += usage.input_tokens
    _USAGE["output"] += usage.output_tokens
    _USAGE["cache_read"] += cache_read
    _USAGE["cache_creation"] += cache_creation
    return cache_read, cache_creation


def _classify_context_rejection(
    error: APIStatusError,
) -> context_management.RecoveryKind | None:
    """Classify recoverable token/byte rejections without swallowing other errors."""
    status_code = getattr(error, "status_code", None)
    if status_code == 413:
        return context_management.RecoveryKind.BYTES
    if status_code != 400:
        return None
    details = " ".join(
        str(part)
        for part in (
            getattr(error, "message", ""),
            getattr(error, "body", ""),
        )
        if part
    ).lower()
    if "prompt is too long" in details:
        return context_management.RecoveryKind.TOKENS
    return None


def summary_runtime() -> context_management.SummaryRuntime:
    """Bind API callbacks needed by context-management summary requests.

    Summary calls reuse the live request shape and usage accounting but send
    through the raw Messages client, avoiding recursive entry into
    :func:`llm_response`.
    """
    return context_management.SummaryRuntime(
        default_model=MODEL,
        default_tools=TOOLS,
        build_system_block=_build_system_block,
        cacheable=_cacheable,
        thinking_param=_thinking_param,
        build_request_params=_build_request_params,
        create_message=client.messages.create,
        record_usage=_record_usage,
    )


def llm_response(
    messages,
    system: str | None = None,
    stream: bool = True,
    tools=None,
    model: str | None = None,
    session_id: str | None = None,
    ctx: "context_management.ContextState | None" = None,
):
    """Send one model turn with proactive shrinking and one reactive retry.

    The context manager may rewrite ``messages`` in place before the first send
    or while recovering a structural 400/413 rejection. Other API errors escape
    unchanged. A caller should pass one ``ContextState`` per conversation;
    omitting it creates an isolated, single-call state so nested requests cannot
    corrupt the parent conversation's token anchor.
    """
    chosen_model = model if model is not None else MODEL
    ctx = ctx if ctx is not None else context_management.ContextState()
    runtime = summary_runtime()
    context_management.prepare_for_request(
        ctx,
        messages,
        model=chosen_model,
        system=system,
        tools=tools,
        session_id=session_id,
        runtime=runtime,
    )

    params = _build_request_params(
        messages,
        model=chosen_model,
        system=system,
        tools=tools,
    )
    sent_message_tokens = context_management.estimate_tokens(messages)
    sent_request_tokens = (
        context_management.estimate_request_input_tokens(params)
    )
    try:
        response = _send_request(params, stream)
    except APIStatusError as error:
        recovery = _classify_context_rejection(error)
        if recovery is None:
            raise
        recovered = context_management.recover_rejected_context(
            ctx,
            messages,
            recovery=recovery,
            model=chosen_model,
            system=system,
            tools=tools,
            session_id=session_id,
            runtime=runtime,
        )
        if not recovered:
            raise
        params = _build_request_params(
            messages,
            model=chosen_model,
            system=system,
            tools=tools,
        )
        sent_message_tokens = context_management.estimate_tokens(messages)
        sent_request_tokens = (
            context_management.estimate_request_input_tokens(params)
        )
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
        sent_request_tokens,
        model=chosen_model,
    )
    return response
