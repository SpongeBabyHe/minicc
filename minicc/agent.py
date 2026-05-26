from minicc.llm import llm_response
from minicc.tools import TOOL_HANDLERS
from minicc.permissions import confirm


def _fmt_args(args: dict, cap: int = 80) -> str:
    parts = []
    for k, v in args.items():
        s = repr(v)
        if len(s) > cap:
            s = s[:cap] + f"...[+{len(s) - cap}]"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _fmt_result(output, cap: int = 300) -> str:
    s = str(output)
    if len(s) > cap:
        return s[:cap] + f"\n         ...[+{len(s) - cap} more chars]"
    return s


def agent_loop(messages):
    while True:
        response = llm_response(messages)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\n[CALL]   {block.name}({_fmt_args(block.input)})")
                handler = TOOL_HANDLERS.get(block.name)
                if handler is None:
                    output = f"Unknown tool: {block.name}"
                elif not confirm(block.name, block.input):   # harness permission
                    output = f"User declined to run {block.name}."
                else:
                    output = handler(**block.input)
                print(f"[RESULT] {_fmt_result(output)}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output})
        messages.append({"role": "user", "content": results})
