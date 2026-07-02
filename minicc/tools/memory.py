from minicc import memory as _store

SCHEMA = {
    "name": "memory",
    "description": (
        "Persistent cross-session memory over a /memories store: a MEMORY.md index "
        "(loaded into every session) plus one-fact-per-topic files read on demand. "
        "The index is already injected each session; use this tool to READ a topic "
        "file (view) and to RECORD durable, reusable learnings as you work: user "
        "preferences, decisions, project facts, fixes/gotchas — NOT transient task "
        "state. Keep MEMORY.md a concise index and add a one-line pointer there when "
        "you create a topic file. Topic files use this shape:\n"
        "---\nname: <kebab-slug>\ndescription: <one line, used for recall>\n"
        "metadata:\n  type: user | feedback | project | reference\n---\n"
        "<the fact; link related memories with [[their-name]]>\n"
        "Keep memory organized — update an existing file rather than duplicating; "
        "writes (create/str_replace) ask the user for approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["view", "create", "str_replace"],
                "description": "view a file/dir, create (or overwrite) a file, or str_replace within a file.",
            },
            "path": {
                "type": "string",
                "description": "A path under /memories, e.g. /memories or /memories/<slug>.md",
            },
            "file_text": {"type": "string", "description": "For create: the full file contents."},
            "old_str": {
                "type": "string",
                "description": "For str_replace: exact text to replace; must match one unique location.",
            },
            "new_str": {
                "type": "string",
                "description": "For str_replace: replacement text (omit to delete old_str).",
            },
            "view_range": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "For view: [start, end] line range ([start, -1] to end).",
            },
        },
        "required": ["command", "path"],
    },
}


def memory(command, path, file_text=None, old_str=None, new_str="", view_range=None):
    if command == "view":
        return _store.view(path, view_range)
    if command == "create":
        if file_text is None:
            return "Error: create requires file_text"
        return _store.create(path, file_text)
    if command == "str_replace":
        if old_str is None:
            return "Error: str_replace requires old_str"
        return _store.str_replace(path, old_str, new_str or "")
    return f"Error: unknown command {command!r} (use view, create, or str_replace)"
