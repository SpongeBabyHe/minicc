"""task_get — CC's TaskGet. See minicc/tasks.py."""

from minicc import tasks

SCHEMA = {
    "name": "task_get",
    "description": (
        "Retrieve one task's full detail by id — description, owner, and its "
        "blocks / blockedBy dependencies. Read this before starting work on a "
        "task; verify its blockedBy list is empty first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string", "description": "The id of the task to retrieve."},
        },
        "required": ["taskId"],
    },
}


def task_get(taskId):
    rec = tasks.get(str(taskId))
    if rec is None:
        return f"Error: no task #{taskId}."
    return tasks.render_detail(rec)
