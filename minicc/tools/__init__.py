from minicc import config
from . import bash, read_file, write_file, edit_file, glob, grep, agent, memory, skill, web_fetch, web_search
from . import task_create, task_list, task_get, task_update

# `agent` (tools/agent.py) is CC's Agent spawn tool; the Task* tools are the
# separate coordination task-list (D1) — a stateful, id-keyed store with a
# dependency graph. See tools/agent.py, minicc/agents.py, minicc/tasks.py.
_BASE_MODULES = [
    bash, read_file, write_file, edit_file, glob, grep, agent,
    task_create, task_list, task_get, task_update,
    memory, skill, web_fetch,
]
# web_search is SERVER-executed (no client handler; see tools/web_search.py). It's
# offered unless settings disable it ("web_search": false) — necessary because an
# org that disabled web search in the Console 400s any request that includes it.
_ALL_MODULES = [*_BASE_MODULES, web_search]

# Tools carry NO cache breakpoint of their own. The request renders in the order
# tools -> system -> messages, so the system-prompt breakpoint's prefix already
# covers every tool — caching tools + system as one stable layer, the way Claude
# Code groups them (system prompt = core instructions + tool definitions). This
# keeps the request within the 4-breakpoint budget while freeing a slot for the
# conversation (system + project-context + conversation). See CONTEXT_MANAGEMENT.md
# § Prompt caching and How Claude Code uses prompt caching.
TOOLS = [module.SCHEMA for module in _ALL_MODULES]
# Server-executed tools (web_search) have no handler function — the API runs them
# inside the request and their results arrive as assistant content blocks.
TOOL_HANDLERS = {
    module.SCHEMA["name"]: getattr(module, module.SCHEMA["name"])
    for module in _ALL_MODULES
    if hasattr(module, module.SCHEMA["name"])
}


def configure_from_settings() -> None:
    """Update the shared schema list from the active source-filtered settings."""
    modules = list(_BASE_MODULES)
    if config.web_search_enabled():
        modules.append(web_search)
    TOOLS[:] = [module.SCHEMA for module in modules]

# Sub-agent tool subsets are no longer defined here: each AgentDef carries its
# own allowlist (agents.py — the built-in `explore` type is the read-only set),
# and tools/agent.py derives the schemas per spawn, always minus `agent` (D6).
