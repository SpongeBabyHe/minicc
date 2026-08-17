from dataclasses import dataclass
from enum import Enum

from minicc.llm import llm_response
from minicc.tools import TOOLS, TOOL_HANDLERS
from minicc import context_management, permissions, ux
from minicc import checkpoints
from minicc import sessions
from minicc import hooks
from minicc import session_limits


# Runaway guard for the Stop hook, straight from CC's best-practices doc: "Claude
# Code overrides the hook and ends the turn after 8 consecutive blocks."
MAX_STOP_BLOCKS = 8

# Provider-level reasons that map to the same agent-loop transition. A normal
# model stop is only a completion *candidate*: the Stop hook still decides
# whether the loop may actually end.
_COMPLETION_CANDIDATE_REASONS = frozenset({"end_turn", "stop_sequence"})
_TRUNCATION_REASONS = frozenset(
    {"max_tokens", "model_context_window_exceeded"}
)


class TurnStatus(str, Enum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    REFUSED = "refused"
    LIMIT_REACHED = "limit_reached"
    STOPPED = "stopped"


@dataclass(frozen=True)
class TurnOutcome:
    status: TurnStatus
    reason: str
    output_text: str = ""
    model_turns: int = 0

    @property
    def completed(self) -> bool:
        return self.status is TurnStatus.COMPLETED


class _StopAction(str, Enum):
    ACCEPT = "accept"
    RETRY = "retry"
    STOPPED = "stopped"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True)
class _StopDecision:
    action: _StopAction
    reason: str = ""


def _block_type(block) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _content_text(
    content, *, separator: str = "\n", strip: bool = True
) -> str:
    """Extract text from SDK or serialized blocks.

    Outcomes use newline-separated, trimmed display text. Stop hooks opt into
    the provider's former exact concatenation shape for compatibility.
    """
    parts = []
    for block in content or []:
        if _block_type(block) != "text":
            continue
        text = block.get("text", "") if isinstance(block, dict) else getattr(
            block, "text", ""
        )
        if text:
            parts.append(str(text))
    result = separator.join(parts)
    return result.strip() if strip else result


def _split_tool_use_blocks(content) -> tuple[list, list]:
    """Return client tool calls and all remaining content, in original order."""
    tool_blocks = []
    other_blocks = []
    for block in content:
        if _block_type(block) == "tool_use":
            tool_blocks.append(block)
        else:
            other_blocks.append(block)
    return tool_blocks, other_blocks


def agent_loop(
    messages,
    system: str | None = None,
    stream: bool = True,
    tools=None,
    max_turns: int | None = None,
    indent: str = "",
    model: str | None = None,
    session_id: str | None = None,
    ctx: context_management.ContextState | None = None,
) -> TurnOutcome:
    """Run until the model reaches a typed terminal outcome.

    ``COMPLETED`` means the model protocol ended normally and the Stop gate
    accepted it. It does not claim that the user's task was verified correct.
    Operational failures and interrupts still raise so the caller keeps its
    existing rollback boundary.

    tools     : tool schemas to advertise (default: all TOOLS). Sub-agents pass
                a read-only subset.
    max_turns : cap the number of model turns (sub-agents pass a limit so a
                runaway exploration can't loop forever).
    indent    : prefix for tool-call/result lines, so a sub-agent's activity
                nests visually under the parent's `agent(...)` call.
    model     : per-call model override (sub-agents run on a cheaper model);
                None = the global MODEL. Threaded to llm_response without
                mutating the global, so the parent's cache/model are untouched.
    ctx       : this conversation's context-trigger state.
                The main session passes its persistent one; omitted (sub-agents,
                /memory consolidate) a fresh one is created here — one per
                conversation, so loops never share trigger state.
    """
    tools = permissions.filter_tools(tools if tools is not None else TOOLS)
    ctx = ctx if ctx is not None else context_management.ContextState()
    limits = session_limits.SessionLimits.load(session_id)
    # guard: model can't call un-advertised tools
    allowed = {t["name"] for t in tools}
    # consecutive Stop-hook blocks this turn (capped, see _stop_gate)
    stop_blocks = 0
    turns = 0
    last_output = ""
    while True:
        if max_turns is not None and turns >= max_turns:
            # Say it out loud: a sub-agent cut off here may still have emitted
            # text last turn. The typed result keeps that text explicitly partial.
            ux.say(
                f"{indent}[stopped at the {max_turns}-turn limit — "
                "the work may be incomplete]",
                style=ux.S_ERROR,
            )
            return TurnOutcome(
                TurnStatus.LIMIT_REACHED,
                "max_turns",
                last_output,
                turns,
            )
        turns += 1
        # streaming shows its own spinner-until-first-token, so no ux.thinking()
        response = llm_response(
            messages,
            system,
            stream=stream,
            tools=limits.tools_for_request(tools),
            model=model,
            session_id=session_id,
            ctx=ctx,
        )
        limits.record_response(response)
        assistant_msg = {"role": "assistant", "content": response.content}
        messages.append(assistant_msg)
        last_output = _content_text(response.content)
        stop_reason = response.stop_reason

        # The provider exposes several stop reasons, but the loop has four
        # transitions: resume a server turn, refuse, run client tools, or finish
        # a non-tool response. Keep that state-machine split visible here.
        if stop_reason == "pause_turn":
            # A long-running SERVER tool turn (web_search) was paused mid-flight.
            # Contract: send the paused assistant message back unchanged and the
            # API resumes it — so record it and loop again without tool results.
            if session_id:
                sessions.append_message(
                    session_id, assistant_msg, usage=response.usage)
            continue

        if stop_reason == "refusal":
            # A safety classifier declined (HTTP 200, not an error). A pre-output
            # refusal carries an EMPTY content array, and an empty assistant
            # message is invalid as next-turn input — letting it into history (or
            # the transcript) poisons both, the same class of bug as a dangling
            # tool_use. So roll it back, tell the user, and end the turn without
            # recording it or running the Stop hook (the turn never really ran).
            messages.pop()
            ux.say(
                f"{indent}the model declined this request "
                "(stop_reason=refusal) — nothing was recorded; try rephrasing",
                style=ux.S_ERROR,
            )
            return TurnOutcome(TurnStatus.REFUSED, "refusal", "", turns)

        tool_blocks, non_tool_blocks = _split_tool_use_blocks(response.content)

        if stop_reason == "tool_use":
            if not tool_blocks:
                # Never append an empty tool_result message: it would make the
                # next API request invalid and disguise a provider-protocol
                # mismatch as a productive round.
                if response.content:
                    if session_id:
                        sessions.append_message(
                            session_id, assistant_msg, usage=response.usage
                        )
                else:
                    messages.pop()
                ux.say(
                    f"{indent}response stopped for tool use but supplied no "
                    "tool call — the work may be incomplete",
                    style=ux.S_ERROR,
                )
                return TurnOutcome(
                    TurnStatus.INCOMPLETE,
                    "tool_use_without_tool_block",
                    last_output,
                    turns,
                )

            results = _execute_tool_blocks(
                tool_blocks,
                allowed,
                session_id,
                indent,
                limits,
            )
            tool_msg = {"role": "user", "content": results}
            messages.append(tool_msg)
            # A productive round breaks the Stop-hook block CHAIN: CC's guard is
            # "8 CONSECUTIVE blocks", so real work restores the hook's budget.
            stop_blocks = 0
            # Persist assistant + tool results only after every tool completed,
            # so an interrupt cannot leave a dangling tool_use in the transcript.
            if session_id:
                sessions.append_message(
                    session_id, assistant_msg, usage=response.usage
                )
                sessions.append_message(session_id, tool_msg)
            continue

        # Every remaining stop reason ends this provider response without a
        # legitimate client-tool round. It may still carry a partial tool_use if
        # generation was cut off mid-call; remove that block before persistence.
        discarded_tool_call = bool(tool_blocks)
        if discarded_tool_call:
            assistant_msg["content"] = non_tool_blocks or [
                {
                    "type": "text",
                    "text": "[response truncated at the output-token limit]",
                }
            ]
            ux.say(
                f"{indent}response stopped ({stop_reason}) mid tool-call; "
                "the partial call was discarded — say 'continue' to resume",
                style=ux.S_ERROR,
            )
            last_output = _content_text(assistant_msg["content"])
        elif stop_reason in _TRUNCATION_REASONS:
            # Content is valid, but ending silently would disguise a cut-off
            # response as a finished one.
            ux.say(
                f"{indent}response was cut off ({stop_reason}) "
                "— say 'continue' to resume",
                style=ux.S_ERROR,
            )

        if session_id:  # terminal assistant → record alone
            sessions.append_message(
                session_id, assistant_msg, usage=response.usage
            )

        # Partial tool calls and truncation are never completion candidates.
        if discarded_tool_call:
            reason = stop_reason or "partial_tool_use"
            if reason not in _TRUNCATION_REASONS:
                reason = f"{reason}_with_tool_use"
            return TurnOutcome(
                TurnStatus.INCOMPLETE, reason, last_output, turns
            )

        if stop_reason in _TRUNCATION_REASONS:
            return TurnOutcome(
                TurnStatus.INCOMPLETE,
                stop_reason,
                last_output,
                turns,
            )

        if stop_reason in _COMPLETION_CANDIDATE_REASONS:
            # A normal provider stop is only a completion candidate. A Stop hook
            # may feed back a reason and continue, stop explicitly, or hit its
            # bounded retry limit.
            decision = _stop_gate(
                response, messages, session_id, indent, stop_blocks
            )
            if decision.action is _StopAction.RETRY:
                stop_blocks += 1
                continue
            if decision.action is _StopAction.STOPPED:
                return TurnOutcome(
                    TurnStatus.STOPPED,
                    decision.reason or "stop_hook",
                    last_output,
                    turns,
                )
            if decision.action is _StopAction.LIMIT_REACHED:
                return TurnOutcome(
                    TurnStatus.LIMIT_REACHED,
                    decision.reason or "stop_hook_limit",
                    last_output,
                    turns,
                )
            return TurnOutcome(
                TurnStatus.COMPLETED,
                stop_reason,
                last_output,
                turns,
            )

        # Unknown provider values fail closed: preserve the raw reason for UX,
        # but never let a newly introduced value silently mean success.
        reason = stop_reason or "unknown_stop_reason"
        ux.say(
            f"{indent}response stopped with an unrecognized reason "
            f"({reason}) — the work may be incomplete",
            style=ux.S_ERROR,
        )
        return TurnOutcome(
            TurnStatus.INCOMPLETE, reason, last_output, turns
        )


def _execute_tool_blocks(
    tool_blocks,
    allowed,
    session_id,
    indent,
    limits: session_limits.SessionLimits,
) -> list[dict]:
    """Execute one complete client-tool round and build its tool results.

    Transcript persistence deliberately remains in ``agent_loop`` and is
    deferred until every tool has returned successfully.
    """
    results = []
    for block in tool_blocks:
        ux.say(
            f"{indent}→ {block.name}({ux.fmt_dict(block.input)})",
            style=ux.S_CALL,
        )
        output = _run_tool(
            block,
            allowed,
            session_id,
            indent,
            limits=limits,
        )
        result = ux.truncate(output, 300)
        prefixed = f"{indent}← " + result.replace("\n", f"\n{indent}  ")
        ux.say(prefixed, style=ux.S_RESULT)
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            }
        )
    return results


def _stop_gate(
    response, messages, session_id, indent, blocks_so_far
) -> _StopDecision:
    """Run Stop hooks for a candidate completion and return a typed decision.

    CC contract: stdin carries `last_assistant_message` (the final assistant text —
    hooks read it instead of tailing the transcript); exit 2 / decision:"block"
    prevents stopping and feeds stderr/reason to the model; `continue:false`
    overrides a block and ends the turn; additionalContext WITHOUT a block enters
    the conversation but the turn still ends. After MAX_STOP_BLOCKS consecutive
    blocks the hook is overridden (CC's runaway guard).

    Main session only: sub-agents pass session_id=None and skip this — their
    turn-end is CC's separate SubagentStop event, which minicc doesn't wire.
    """
    if not session_id:  # sub-agents skip this
        return _StopDecision(_StopAction.ACCEPT)
    last_text = _content_text(response.content, separator="", strip=False)
    d = hooks.run(
        "Stop",
        session_id=session_id,
        last_assistant_message=last_text,
        stop_hook_active=blocks_so_far > 0,
    )
    hooks.surface(d, indent)
    if d.stop and d.stop_reason:
        ux.say(f"{indent}[Stop hook: {d.stop_reason}]", style=ux.S_INFO)

    # continue:false is universal and therefore wins over Stop's event-specific
    # block decision. Preserve additional context just as the former bool path
    # did, but report that this was not a normal accepted completion.
    if d.stop:
        if d.additional_context:
            ctx = {"role": "user", "content": "\n".join(d.additional_context)}
            messages.append(ctx)
            sessions.append_message(session_id, ctx)
        return _StopDecision(
            _StopAction.STOPPED, d.stop_reason or "stop_hook"
        )

    if d.block and blocks_so_far >= MAX_STOP_BLOCKS:
        ux.say(
            f"{indent}[Stop hook still blocking after {MAX_STOP_BLOCKS} attempts — "
            "ending the turn as incomplete]",
            style=ux.S_INFO,
        )
        return _StopDecision(
            _StopAction.LIMIT_REACHED, "stop_hook_limit"
        )

    if d.block:
        reason = d.reason or "A Stop hook blocked ending the turn (no reason given)."
        note = f"[Stop hook]: {reason}"
        if d.additional_context:
            note += "\n\n" + "\n".join(d.additional_context)
        ux.say(
            f"{indent}[Stop hook blocked stopping — continuing]", style=ux.S_INFO)
        fb = {"role": "user", "content": note}
        messages.append(fb)
        sessions.append_message(session_id, fb)  # session_id is non-empty here
        return _StopDecision(_StopAction.RETRY, "stop_hook_block")

    if d.additional_context:
        # Not blocked: context still enters the conversation (visible to the model
        # from the next turn) but the turn ends — same trailing-user-message shape
        # as UserPromptSubmit context, which the API accepts (live-verified).
        ctx = {"role": "user", "content": "\n".join(d.additional_context)}
        messages.append(ctx)
        sessions.append_message(session_id, ctx)
    return _StopDecision(_StopAction.ACCEPT)


def _run_tool(
    block,
    allowed,
    session_id,
    indent,
    limits: session_limits.SessionLimits | None = None,
) -> str:
    """Execute one tool call, wrapping the permission gate + handler in PreToolUse and
    PostToolUse hooks. Returns the tool_result content (with any hook-injected context
    appended). PreToolUse can deny the call, rewrite its input, force a prompt, or
    pre-approve it subject to settings rules; PostToolUse can feed context back or
    replace the output."""
    pre = hooks.run(
        "PreToolUse",
        session_id=session_id,
        match_value=block.name,
        tool_name=block.name,
        tool_input=block.input,
        tool_use_id=block.id,
    )
    hooks.surface(pre, indent)
    hooks.raise_if_stopped(pre, "PreToolUse")

    handler = TOOL_HANDLERS.get(block.name)
    tool_input = pre.updated_input if pre.updated_input is not None else block.input

    if pre.block:
        output = f"Blocked by a PreToolUse hook. {pre.reason or ''}".rstrip()
    elif handler is None:  # no such tool anywhere — the model invented the name
        output = f"Unknown tool: {block.name}"
    elif block.name not in allowed:
        # Real tool, just not advertised to THIS agent (e.g. a read-only
        # sub-agent reaching for bash). Saying "unknown" would be misleading and
        # invites a retry; name the real reason so the model can re-plan.
        output = f"Tool {block.name} is not available to this agent."
    elif not (
        authorization := permissions.authorize(
            block.name,
            tool_input,
            hook_allow=pre.allow,
            hook_ask=pre.ask,
        )
    ).allowed:
        output = authorization.reason or f"Authorization denied {block.name}."
    elif (
        block.name == "agent"
        and limits is not None
        and not limits.claim_subagent()
    ):
        output = limits.subagent_limit_result()
    else:
        try:
            if block.name in ("write_file", "edit_file"):
                checkpoints.before_write(tool_input.get("path"))  # for /rewind
        except checkpoints.CheckpointError as error:
            output = (
                "Error: could not create a rewind checkpoint; "
                f"the file was not modified: {error}"
            )
        else:
            try:
                output = handler(**tool_input)
            except Exception as e:
                output = f"Error: tool crashed: {e!r}"
            output = _post_tool(block, tool_input, output, session_id, indent)

    if pre.additional_context:
        output += "\n\n" + "\n".join(pre.additional_context)
    return output


def _post_tool(block, tool_input, output, session_id, indent) -> str:
    """Run PostToolUse for a tool that actually executed. The hook can't un-run the
    tool, but it can replace the result (updatedToolOutput), feed the model a note
    (decision:block reason / additionalContext), or warn the user (systemMessage)."""
    post = hooks.run(
        "PostToolUse",
        session_id=session_id,
        match_value=block.name,
        tool_name=block.name,
        tool_input=tool_input,
        tool_use_id=block.id,
        tool_response={"type": "text", "text": output},
    )
    hooks.surface(post, indent)
    hooks.raise_if_stopped(post, "PostToolUse")
    if post.updated_output is not None:
        output = post.updated_output
    notes = list(post.additional_context)
    if post.block and post.reason:
        notes.append(f"[PostToolUse hook]: {post.reason}")
    if notes:
        output += "\n\n" + "\n".join(notes)
    return output
