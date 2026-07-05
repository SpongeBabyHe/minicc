from minicc import config
from . import bash, read_file, write_file, edit_file, glob, grep, task, todo_write, memory, web_fetch, web_search

_MODULES = [bash, read_file, write_file, edit_file, glob, grep, task, todo_write, memory, web_fetch]
# web_search is SERVER-executed (no client handler; see tools/web_search.py). It's
# offered unless settings disable it ("web_search": false) — necessary because an
# org that disabled web search in the Console 400s any request that includes it.
if config.web_search_enabled():
    _MODULES.append(web_search)

# Tools carry NO cache breakpoint of their own. The request renders in the order
# tools -> system -> messages, so the system-prompt breakpoint's prefix already
# covers every tool — caching tools + system as one stable layer, the way Claude
# Code groups them (system prompt = core instructions + tool definitions). This
# keeps the request within the 4-breakpoint budget while freeing a slot for the
# conversation (system + project-context + conversation). See CONTEXT_MANAGEMENT.md
# § Prompt caching and How Claude Code uses prompt caching.
TOOLS = [m.SCHEMA for m in _MODULES]
# Server-executed tools (web_search) have no handler function — the API runs them
# inside the request and their results arrive as assistant content blocks.
TOOL_HANDLERS = {
    m.SCHEMA["name"]: getattr(m, m.SCHEMA["name"])
    for m in _MODULES
    if hasattr(m, m.SCHEMA["name"])
}

# Read-only subset for sub-agents (SUBAGENTS.md D3): no bash/write/edit, and no
# `task` either — sub-agents don't spawn nested sub-agents. Read-only tools are
# ungated, so a sub-agent never triggers a permission prompt (resolves D4).
_READ_ONLY_NAMES = {"read_file", "glob", "grep"}
READ_ONLY_TOOLS = [t for t in TOOLS if t["name"] in _READ_ONLY_NAMES]
