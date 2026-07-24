from minicc.llm import llm_response
from minicc.tools import TOOLS, TOOL_HANDLERS
from minicc.permissions import confirm
from minicc import compact, ux
from minicc import checkpoints
from minicc import sessions
from minicc import hooks

# Runaway guard for the Stop hook, straight from CC's best-practices doc: "Claude
# Code overrides the hook and ends the turn after 8 consecutive blocks."
MAX_STOP_BLOCKS = 8


def agent_loop(
    messages,
    system: str | None = None,
    stream: bool = True,
    tools=None,
    max_turns: int | None = None,
    indent: str = "",
    model: str | None = None,
    session_id: str | None = None,
    ctx: compact.ContextState | None = None,
):
    """Run the agent loop until the model stops requesting tools.

    tools     : tool schemas to advertise (default: all TOOLS). Sub-agents pass
                a read-only subset.
    max_turns : cap the number of model turns (sub-agents pass a limit so a
                runaway exploration can't loop forever).
    indent    : prefix for tool-call/result lines, so a sub-agent's activity
                nests visually under the parent's `agent(...)` call.
    model     : per-call model override (sub-agents run on a cheaper model);
                None = the global MODEL. Threaded to llm_response without
                mutating the global, so the parent's cache/model are untouched.
    ctx       : this conversation's context-trigger state (compact.ContextState).
                The main session passes its persistent one; omitted (sub-agents,
                /memory consolidate) a fresh one is created here — one per
                conversation, so loops never share trigger state.
    """
    tools = tools if tools is not None else TOOLS
    ctx = ctx if ctx is not None else compact.ContextState()
    allowed = {t["name"] for t in tools}  # guard: model can't call un-advertised tools
    turns = 0
    stop_blocks = 0  # consecutive Stop-hook blocks this turn (capped, see _stop_gate)
    while True:
        if max_turns is not None and turns >= max_turns:
            # Say it out loud: a sub-agent cut off here may still have emitted
            # text last turn, and the caller's _final_text would then hand that
            # partial work up as if it were a finished summary.
            ux.say(
                f"{indent}[stopped at the {max_turns}-turn limit — "
                "the work may be incomplete]",
                style=ux.S_ERROR,
            )
            return
        turns += 1
        # streaming shows its own spinner-until-first-token, so no ux.thinking()
        response = llm_response(
            messages,
            system,
            stream=stream,
            tools=tools,
            model=model,
            session_id=session_id,
            ctx=ctx,
        )
        assistant_msg = {"role": "assistant", "content": response.content}
        messages.append(assistant_msg)
        if response.stop_reason == "pause_turn":
            # A long-running SERVER tool turn (web_search) was paused mid-flight.
            # Contract: send the paused assistant message back unchanged and the
            # API resumes it — so record it and loop again without tool results.
            if session_id:
                sessions.append_message(session_id, assistant_msg, usage=response.usage)
            continue
        if response.stop_reason == "refusal":
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
            return
        if response.stop_reason != "tool_use":
            # A non-tool_use stop can still CARRY tool_use blocks: max_tokens
            # cutting the stream mid tool-call leaves a partial call with
            # truncated (often empty) input and no result can ever answer it —
            # sending it back 400s the next request ("tool_use ids were found
            # without tool_result blocks"; dogfood R5, output=16000 exactly at
            # the cap). Drop the partial call, keep what streamed before it,
            # and say so out loud instead of ending the turn silently.
            if any(getattr(b, "type", None) == "tool_use" for b in response.content):
                kept = [b for b in response.content if getattr(b, "type", None) != "tool_use"]
                assistant_msg["content"] = kept or [
                    {"type": "text", "text": "[response truncated at the output-token limit]"}
                ]
                ux.say(
                    f"{indent}response stopped ({response.stop_reason}) mid tool-call; "
                    "the partial call was discarded — say 'continue' to resume",
                    style=ux.S_ERROR,
                )
            elif response.stop_reason == "max_tokens":
                # Text-only truncation: the content stays valid, so nothing to
                # discard — but ending silently leaves the user unable to tell a
                # cut-off answer from a finished one.
                ux.say(
                    f"{indent}response hit the output-token limit and was cut off "
                    "— say 'continue' to resume",
                    style=ux.S_ERROR,
                )
            if session_id:  # terminal assistant → record alone
                sessions.append_message(session_id, assistant_msg, usage=response.usage)
            # Stop hook: the deterministic turn-end gate (the enforced complement to
            # the verify-work stance). A block feeds its reason back and keeps the
            # turn going; MAX_STOP_BLOCKS caps a hook that never lets go.
            if _stop_gate(response, messages, session_id, indent, stop_blocks):
                stop_blocks += 1
                continue
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                ux.say(
                    f"{indent}→ {block.name}({ux.fmt_dict(block.input)})",
                    style=ux.S_CALL,
                )
                output = _run_tool(block, allowed, session_id, indent)
                result = ux.truncate(output, 300)
                prefixed = f"{indent}← " + result.replace("\n", f"\n{indent}  ")
                ux.say(prefixed, style=ux.S_RESULT)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        tool_msg = {"role": "user", "content": results}
        messages.append(tool_msg)
        # A productive round breaks the Stop-hook block CHAIN: CC's guard is "8
        # CONSECUTIVE blocks", so real work between blocks restores the hook's
        # full budget instead of letting a cumulative count silently retire it.
        stop_blocks = 0
        # Record assistant + its tool_results together, only now that both exist —
        # so a Ctrl-C mid-tool never persists a dangling tool_use to the transcript.
        if session_id:
            sessions.append_message(session_id, assistant_msg, usage=response.usage)
            sessions.append_message(session_id, tool_msg)


def _stop_gate(response, messages, session_id, indent, blocks_so_far) -> bool:
    """Run Stop hooks when a turn is about to end. Returns True if the turn must
    CONTINUE (a hook blocked the stop; its reason is appended to `messages` as a
    user message the model answers next iteration), False to end the turn.

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
        return False
    last_text = "".join(
        b.text for b in response.content if getattr(b, "type", "") == "text"
    )
    d = hooks.run("Stop", session_id=session_id, last_assistant_message=last_text)
    hooks.surface(d, indent)
    if d.stop and d.stop_reason:
        ux.say(f"{indent}[Stop hook: {d.stop_reason}]", style=ux.S_INFO)

    # `and not d.stop`: for Stop these are OPPOSITES — block says "keep going",
    # continue:false says "end now" — so continue:false wins (CC precedence).
    if d.block and not d.stop and blocks_so_far >= MAX_STOP_BLOCKS:
        ux.say(
            f"{indent}[Stop hook still blocking after {MAX_STOP_BLOCKS} attempts — "
            "ending the turn anyway]",
            style=ux.S_INFO,
        )
        return False

    if d.block and not d.stop:  # continue:false wins over a block (CC precedence)
        reason = d.reason or "A Stop hook blocked ending the turn (no reason given)."
        note = f"[Stop hook]: {reason}"
        if d.additional_context:
            note += "\n\n" + "\n".join(d.additional_context)
        ux.say(f"{indent}[Stop hook blocked stopping — continuing]", style=ux.S_INFO)
        fb = {"role": "user", "content": note}
        messages.append(fb)
        sessions.append_message(session_id, fb)  # session_id is non-empty here
        return True

    if d.additional_context:
        # Not blocked: context still enters the conversation (visible to the model
        # from the next turn) but the turn ends — same trailing-user-message shape
        # as UserPromptSubmit context, which the API accepts (live-verified).
        ctx = {"role": "user", "content": "\n".join(d.additional_context)}
        messages.append(ctx)
        sessions.append_message(session_id, ctx)
    return False


def _run_tool(block, allowed, session_id, indent) -> str:
    """Execute one tool call, wrapping the permission gate + handler in PreToolUse and
    PostToolUse hooks. Returns the tool_result content (with any hook-injected context
    appended). PreToolUse can deny the call, rewrite its input, force a prompt, or
    pre-approve it; PostToolUse can feed context back or replace the output."""
    pre = hooks.run(
        "PreToolUse",
        session_id=session_id,
        match_value=block.name,
        tool_name=block.name,
        tool_input=block.input,
    )
    hooks.surface(pre, indent)

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
    elif not (pre.allow or confirm(block.name, tool_input, force=pre.ask)):
        output = f"User declined to run {block.name}."
    else:
        if block.name in ("write_file", "edit_file"):
            checkpoints.before_write(tool_input.get("path"))  # for /rewind
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
        tool_response={"type": "text", "text": output},
    )
    hooks.surface(post, indent)
    if post.updated_output is not None:
        output = post.updated_output
    notes = list(post.additional_context)
    if post.block and post.reason:
        notes.append(f"[PostToolUse hook]: {post.reason}")
    if notes:
        output += "\n\n" + "\n".join(notes)
    return output
