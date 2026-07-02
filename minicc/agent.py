from minicc.llm import llm_response
from minicc.tools import TOOLS, TOOL_HANDLERS
from minicc.permissions import confirm
from minicc import ux
from minicc import checkpoints
from minicc import sessions


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
    allowed = {t["name"] for t in tools}   # guard: model can't call un-advertised tools
    turns = 0
    while True:
        if max_turns is not None and turns >= max_turns:
            return
        turns += 1
        # streaming shows its own spinner-until-first-token, so no ux.thinking()
        response = llm_response(
            messages, system, stream=stream, tools=tools, model=model, session_id=session_id
        )
        assistant_msg = {"role": "assistant", "content": response.content}
        messages.append(assistant_msg)
        if response.stop_reason != "tool_use":
            if session_id:                        # terminal assistant → record alone
                sessions.append_message(session_id, assistant_msg)
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                ux.say(
                    f"{indent}→ {block.name}({ux.fmt_dict(block.input)})",
                    style=ux.S_CALL,
                )
                handler = TOOL_HANDLERS.get(block.name) if block.name in allowed else None
                if handler is None:
                    output = f"Unknown tool: {block.name}"
                elif not confirm(block.name, block.input):  # harness permission
                    output = f"User declined to run {block.name}."
                else:
                    if block.name in ("write_file", "edit_file"):
                        checkpoints.before_write(block.input.get("path"))  # for /rewind
                    try:
                        output = handler(**block.input)
                    except Exception as e:
                        output = f"Error: tool crashed: {e!r}"
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
            sessions.append_message(session_id, assistant_msg)
            sessions.append_message(session_id, tool_msg)
