from minicc.llm import llm_response
from minicc.tools import TOOLS, TOOL_HANDLERS
from minicc.permissions import confirm
from minicc import ux


def agent_loop(
    messages,
    system: str | None = None,
    stream: bool = True,
    tools=None,
    max_turns: int | None = None,
    indent: str = "",
):
    """Run the agent loop until the model stops requesting tools.

    tools     : tool schemas to advertise (default: all TOOLS). Sub-agents pass
                a read-only subset.
    max_turns : cap the number of model turns (sub-agents pass a limit so a
                runaway exploration can't loop forever).
    indent    : prefix for tool-call/result lines, so a sub-agent's activity
                nests visually under the parent's `task(...)` call.
    """
    tools = tools if tools is not None else TOOLS
    allowed = {t["name"] for t in tools}   # guard: model can't call un-advertised tools
    turns = 0
    while True:
        if max_turns is not None and turns >= max_turns:
            return
        turns += 1
        # streaming shows its own spinner-until-first-token, so no ux.thinking()
        response = llm_response(messages, system, stream=stream, tools=tools)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
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
        messages.append({"role": "user", "content": results})
