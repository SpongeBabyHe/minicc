"""task_create — CC's TaskCreate (the coordination substrate; replaces todo_write)."""

from minicc import tasks

SCHEMA = {
    "name": "task_create",
    "description": (
        "Create a structured task in the task list to track a step of the "
        "current multi-step work. Use for tasks needing 3+ distinct steps, or "
        "when the user gives several things to do. Skip it for a single trivial "
        "step. After creating tasks, use task_update to set dependencies "
        "(addBlocks / addBlockedBy) if some must finish before others. Check "
        "task_list first to avoid duplicates. All tasks start `pending`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "A brief, imperative title (e.g. 'Fix the login bug').",
            },
            "description": {"type": "string", "description": "What needs to be done."},
            "activeForm": {
                "type": "string",
                "description": "Present-continuous form shown while in progress (e.g. 'Fixing the login bug'). Optional.",
            },
        },
        "required": ["subject", "description"],
    },
}


def task_create(subject, description, activeForm=None):
    rec = tasks.create(subject, description, activeForm)
    return f"Created task #{rec['id']}: {rec['subject']} (pending)"
