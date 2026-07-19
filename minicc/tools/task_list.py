"""task_list — CC's TaskList. See minicc/tasks.py."""

from minicc import tasks

SCHEMA = {
    "name": "task_list",
    "description": (
        "List all tasks with id, subject, status, owner, and open blockers. A "
        "task with blockers can't be started until they resolve. Prefer working "
        "tasks in id order (lowest first) — earlier tasks often set up later "
        "ones. Use this to see available work and check progress before creating "
        "duplicates."
    ),
    "input_schema": {"type": "object", "properties": {}},
}


def task_list():
    return tasks.render_list()
