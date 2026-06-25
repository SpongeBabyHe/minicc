"""todo_write — a model-maintained plan for the current task (learn-cc s03).

Stateless by design: the model passes the COMPLETE list each call; this renders
it and hands it back. The plan lives in the conversation (the tool history), not
in module state — the model re-emits the full list to update it. That's the
native-tool-calling way and is robust to context eviction (the latest call always
carries the current plan). A linear terminal has no persistent panel to keep in
sync, so there's nothing for module state to buy here.
"""

_MARK = {"completed": "✓", "in_progress": "▶", "pending": "☐"}

SCHEMA = {
    "name": "todo_write",
    "description": (
        "Maintain a todo list for the current multi-step task. Pass the COMPLETE "
        "list every call — it replaces the previous one. Plan the task up front, "
        "then mark items in_progress/completed as you go so progress stays visible. "
        "Keep exactly one item in_progress at a time. Skip this for trivial "
        "single-step tasks. Each item is {content, status}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    },
}


def todo_write(todos) -> str:
    if not todos:
        return "todos: (empty)"
    lines = [f"{_MARK.get(t.get('status'), '?')} {t.get('content', '')}" for t in todos]
    return "todos:\n" + "\n".join(lines)
