"""task_update — CC's TaskUpdate. See minicc/tasks.py.

Handler param names mirror CC's schema keys verbatim (taskId, activeForm,
addBlocks, addBlockedBy) — the dispatcher calls handler(**tool_input), so the
names must match the wire schema.
"""

from minicc import tasks

SCHEMA = {
    "name": "task_update",
    "description": (
        "Update a task. Set `status` to in_progress when you START it and "
        "completed when you FULLY finish (tests passing, no partial work) — "
        "otherwise keep it in_progress. Claim a task by setting `owner`. Set "
        "dependencies with addBlocks (tasks that can't start until this one "
        "completes) / addBlockedBy (tasks that must complete before this one). "
        "Completing a task auto-unblocks whatever it blocked. Use status "
        "`deleted` to permanently remove a task created in error."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "taskId": {"type": "string", "description": "The id of the task to update."},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "activeForm": {"type": "string"},
            "owner": {"type": "string", "description": "Agent/owner name; claim the task by setting it."},
            "addBlocks": {"type": "array", "items": {"type": "string"}, "description": "Task ids that cannot start until this one completes."},
            "addBlockedBy": {"type": "array", "items": {"type": "string"}, "description": "Task ids that must complete before this one can start."},
        },
        "required": ["taskId"],
    },
}


def task_update(taskId, status=None, subject=None, description=None,
                activeForm=None, owner=None, addBlocks=None, addBlockedBy=None):
    rec = tasks.update(
        str(taskId), status=status, subject=subject, description=description,
        active_form=activeForm, owner=owner,
        add_blocks=addBlocks, add_blocked_by=addBlockedBy,
    )
    if rec is None:
        return f"Error: no task #{taskId}."
    if rec.get("status") == "deleted":
        return f"Deleted task #{taskId}."
    return "Updated:\n" + tasks.render_detail(rec)
