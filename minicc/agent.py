from minicc.llm import llm_response
from minicc.tools import TOOL_HANDLERS
from minicc.permissions import confirm


def agent_loop(messages):
    while True:
        response = llm_response(messages)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"---> {block.name}({block.input})")
                handler = TOOL_HANDLERS.get(block.name)
                if handler is None:
                    output = f"Unknown tool: {block.name}"
                elif not confirm(block.name, block.input):   # harness permission
                    output = f"User declined to run {block.name}."
                else:
                    output = handler(**block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output})
        messages.append({"role": "user", "content": results})
