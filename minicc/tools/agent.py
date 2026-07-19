"""The `agent` tool — delegate a subtask to a sub-agent (CC's Agent tool).

Build step 2: the sub-agent runs its own agent_loop with a FRESH message list
(the spawn `prompt` is the ONLY channel from parent to child — CC's context
isolation), plus a per-agent system prompt, tool subset, and model resolved
from `subagent_type` — a built-in type or a `.minicc/agents/*.md` definition
(see minicc/agents.py). The parent receives only the sub-agent's final summary;
its intermediate tool calls never enter the parent's context (SUBAGENTS.md).

In-process (D3): a sub-agent with write/bash tools prompts for permission in the
lead's terminal (D4 — free in-process). Nested sub-agents are cut (D6): `task`
is removed from every sub-agent's tool set.
"""

from minicc import agents, ux

SUBAGENT_MAX_TURNS = 15


SCHEMA = {
    "name": "agent",
    "description": (
        "Delegate a subtask to a sub-agent that runs in its own context window "
        "and returns only a final summary — keeping THIS conversation's context "
        "clean. Use for work that would otherwise flood the conversation with "
        "file reads, logs, or search results you won't reference again. The spawn "
        "`prompt` is the ONLY thing the sub-agent sees — include every path, "
        "error, and decision it needs. Choose `subagent_type` from the "
        "agent-types system-reminder (default: general-purpose). No nested "
        "sub-agents."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The self-contained task for the sub-agent to perform.",
            },
            "subagent_type": {
                "type": "string",
                "description": (
                    "Which agent type to use — a name from the agent-types "
                    "system-reminder. Default: general-purpose."
                ),
            },
        },
        "required": ["prompt"],
    },
}


def _final_text(messages) -> str:
    """Pull the last assistant text out of the sub-agent's messages."""
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        text = "\n".join(
            getattr(b, "text", "") for b in content
            if getattr(b, "type", None) == "text"
        ).strip()
        if text:
            return text
    return "(sub-agent returned no summary — it may have hit its turn limit)"


def _tool_schemas(names):
    """A sub-agent's tool schemas: its allowlist (or all tools if None), always
    minus `agent` — no nested sub-agents (D6). Read-only sub-agents thus never
    prompt; a general-purpose one's write/bash prompts surface to the lead."""
    from minicc.tools import TOOLS
    pool = [t for t in TOOLS if t["name"] != "agent"]
    if names is None:
        return pool
    wanted = set(names)
    return [t for t in pool if t["name"] in wanted]


def agent(prompt: str, subagent_type: str | None = None) -> str:
    from minicc.query_engine import agent_loop

    adef = agents.resolve(subagent_type)
    if adef is None:
        return (
            f"Error: unknown subagent_type '{subagent_type}'. Valid types are in "
            "the agent-types system-reminder."
        )
    sub = [{"role": "user", "content": prompt}]
    ux.say(f"  [sub-agent '{adef.name}' started]", style=ux.S_INFO)
    agent_loop(
        sub,
        system=adef.prompt,           # the definition's body / built-in prompt
        stream=False,                 # don't stream the sub-agent's internal turns
        tools=_tool_schemas(adef.tools),
        max_turns=SUBAGENT_MAX_TURNS,
        indent="  ",                  # nest its tool lines under the parent
        model=adef.model,             # None = inherit the session model
    )
    ux.say(f"  [sub-agent '{adef.name}' done]", style=ux.S_INFO)
    return _final_text(sub)
