from minicc.llm import llm_response
from minicc.tools import TOOLS, TOOL_HANDLERS
from minicc.permissions import confirm
from minicc import ux
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
):
    """Run the agent loop until the model stops requesting tools.

    tools     : tool schemas to advertise (default: all TOOLS). Sub-agents pass
                a read-only subset.
    max_turns : cap the number of model turns (sub-agents pass a limit so a
                runaway exploration can't loop forever).
    indent    : prefix for tool-call/result lines, so a sub-agent's activity
                nests visually under the parent's `task(...)` call.
    model     : per-call model override (sub-agents run on a cheaper model);
                None = the global MODEL. Threaded to llm_response without
                mutating the global, so the parent's cache/model are untouched.
    """
    tools = tools if tools is not None else TOOLS
    allowed = {t["name"] for t in tools}  # guard: model can't call un-advertised tools
    turns = 0
    stop_blocks = 0  # consecutive Stop-hook blocks this turn (capped, see _stop_gate)
    while True:
        if max_turns is not None and turns >= max_turns:
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
        if response.stop_reason != "tool_use":
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
    for m in d.system_messages:
        ux.say(f"{indent}[hook] {m}", style=ux.S_INFO)
    if d.stop and d.stop_reason:
        ux.say(f"{indent}[Stop hook: {d.stop_reason}]", style=ux.S_INFO)

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
        if session_id:
            sessions.append_message(session_id, fb)
        return True

    if d.additional_context:
        # Not blocked: context still enters the conversation (visible to the model
        # from the next turn) but the turn ends — same trailing-user-message shape
        # as UserPromptSubmit context, which the API accepts (live-verified).
        ctx = {"role": "user", "content": "\n".join(d.additional_context)}
        messages.append(ctx)
        if session_id:
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
    for m in pre.system_messages:
        ux.say(f"{indent}[hook] {m}", style=ux.S_INFO)

    handler = TOOL_HANDLERS.get(block.name) if block.name in allowed else None
    tool_input = pre.updated_input if pre.updated_input is not None else block.input

    if pre.block:
        output = f"Blocked by a PreToolUse hook. {pre.reason or ''}".rstrip()
    elif handler is None:
        output = f"Unknown tool: {block.name}"
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
    for m in post.system_messages:
        ux.say(f"{indent}[hook] {m}", style=ux.S_INFO)
    if post.updated_output is not None:
        output = post.updated_output
    notes = list(post.additional_context)
    if post.block and post.reason:
        notes.append(f"[PostToolUse hook]: {post.reason}")
    if notes:
        output += "\n\n" + "\n".join(notes)
    return output
