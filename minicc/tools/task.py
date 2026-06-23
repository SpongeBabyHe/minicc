"""The `task` tool: delegate a read-only exploration subtask to a sub-agent.

The sub-agent runs its own agent_loop with a fresh message list and a read-only
tool set, in isolation. The parent only receives the sub-agent's final summary —
its dozens of intermediate tool calls never enter the parent's context. See
SUBAGENTS.md.
"""

from minicc import ux

SUBAGENT_MAX_TURNS = 15

SUBAGENT_PROMPT = """You are a focused sub-agent handling a read-only exploration \
subtask for a parent agent.

- Your tools are read-only: read_file, glob, grep. No writing, no bash.
- Investigate the task, then return a CONCISE summary of findings: relevant file
  paths, key facts, and conclusions.
- The parent sees ONLY your final message — not your intermediate tool calls — so
  put everything important in that summary.
- Explore thoroughly, summarize tightly. No preamble, no filler."""


SCHEMA = {
    "name": "task",
    "description": (
        "Delegate a read-only exploration subtask to a sub-agent that has its own "
        "context window. Use ONLY when answering would require reading many files: "
        "the sub-agent explores in isolation and returns a concise summary, keeping "
        "THIS conversation's context clean. Read-only (no edits, no bash, no nested "
        "sub-agents). Returns the sub-agent's summary text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "The subtask for the sub-agent to investigate.",
            }
        },
        "required": ["description"],
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


def task(description: str) -> str:
    # lazy imports: agent imports tools (which imports this module) → avoid cycle
    from minicc.agent import agent_loop
    from minicc.tools import READ_ONLY_TOOLS

    sub = [{"role": "user", "content": description}]
    ux.say("  [sub-agent started]", style=ux.S_INFO)
    agent_loop(
        sub,
        system=SUBAGENT_PROMPT,
        stream=False,                 # don't stream the sub-agent's internal turns
        tools=READ_ONLY_TOOLS,
        max_turns=SUBAGENT_MAX_TURNS,
        indent="  ",                  # nest its tool lines under the parent
    )
    ux.say("  [sub-agent done]", style=ux.S_INFO)
    return _final_text(sub)
