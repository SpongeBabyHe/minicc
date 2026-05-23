from minicc.llm import llm_response
from minicc.tools import TOOL_HANDLERS


def agent_loop(messages):
    while True:
        response = llm_response(messages)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(
                    **block.input) if handler else f"Unknown tool: {block.name}"
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    user_prompt = str(input())
    messages = [{"role": "user", "content": user_prompt}]
    response = agent_loop(messages)
    print(messages)
