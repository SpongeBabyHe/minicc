from minicc.llm import llm_response
from minicc.tools import TOOL_HANDLERS
from minicc.permissions import confirm
from minicc import ux


def agent_loop(messages, system: str | None = None):
    while True:
        with ux.thinking():
            response = llm_response(messages, system)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                ux.say(f"→ {block.name}({ux.fmt_dict(block.input)})", style=ux.S_CALL)
                handler = TOOL_HANDLERS.get(block.name)
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
                # 续行用两空格 indent
                prefixed = "← " + result.replace("\n", "\n  ")
                ux.say(prefixed, style=ux.S_RESULT)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
        messages.append({"role": "user", "content": results})
