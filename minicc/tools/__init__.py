from . import bash, read_file, write_file, edit_file, glob, grep, task, todo_write

_MODULES = [bash, read_file, write_file, edit_file, glob, grep, task, todo_write]

_TOOLS_RAW = [m.SCHEMA for m in _MODULES]


def _with_cache(schemas):
    """Mark the whole tools block cacheable by putting cache_control on the last
    tool. Copies the dict so the module's SCHEMA stays clean for TOOL_HANDLERS."""
    return schemas[:-1] + [{**schemas[-1], "cache_control": {"type": "ephemeral"}}]


TOOLS = _with_cache(_TOOLS_RAW)
TOOL_HANDLERS = {m.SCHEMA["name"]: getattr(m, m.SCHEMA["name"]) for m in _MODULES}

# Read-only subset for sub-agents (SUBAGENTS.md D3): no bash/write/edit, and no
# `task` either — sub-agents don't spawn nested sub-agents. Read-only tools are
# ungated, so a sub-agent never triggers a permission prompt (resolves D4).
_READ_ONLY_NAMES = {"read_file", "glob", "grep"}
READ_ONLY_TOOLS = _with_cache([t for t in _TOOLS_RAW if t["name"] in _READ_ONLY_NAMES])
