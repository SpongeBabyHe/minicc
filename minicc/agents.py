"""Agent definitions — CC's subagent system (CC: .claude/agents/*.md).

Build step 2 of the agents/coordinator plan (docs/CC_AGENTS_COORDINATOR_DESIGN.md):
reusable named sub-agent roles + the built-in types the `agent` tool spawns. A
definition is a flat `.minicc/agents/*.md` file — YAML frontmatter (parsed by the
skills parser) + a body that becomes the agent's system prompt. Discovery mirrors
skills: project (`.minicc/agents` in cwd + ancestors) then personal
(`~/.minicc/agents`), personal winning a name clash; a definition may override a
built-in (CC: "a subagent named Explore overrides the built-in").

Frontmatter, all optional (identity from `name`, else the file stem):
`description`, `tools` (space/comma allowlist of minicc tool names; omitted = all),
`model` (sonnet/opus/haiku/fable/full-id/`inherit`; omitted/inherit = the session
model). The body is the system prompt — with the spawn prompt, the ONLY thing the
sub-agent sees (CC's context isolation: no parent history).

Built-in types (no files needed): `general-purpose` (all tools, inherits the
session model — CC's catch-all) and `explore` (read-only, cheap model — the
original read-only exploration behavior). Nested sub-agents are cut for phase-1
(D6): the caller (`tools/agent.py`) removes `agent` from every sub-agent's tool set.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from minicc import skills  # reuse parse_frontmatter + the discovery-root pattern

# CC's Explore/Plan run read-only on a cheap model; general-purpose inherits.
EXPLORE_MODEL = "claude-haiku-4-5-20251001"

_EXPLORE_PROMPT = """You are a focused sub-agent handling a read-only exploration \
subtask for a parent agent.

- Your tools are read-only: read_file, glob, grep. No writing, no bash.
- Investigate the task, then return a CONCISE summary of findings: relevant file
  paths, key facts, and conclusions.
- The parent sees ONLY your final message — not your intermediate tool calls — so
  put everything important in that summary.
- Explore thoroughly, summarize tightly. No preamble, no filler."""

_GENERAL_PROMPT = """You are a capable sub-agent handling a delegated subtask for \
a parent agent, in your own context window.

- Complete the task fully — the exploration and any edits it requires.
- The parent sees ONLY your final message, not your intermediate tool calls, so
  end with a CONCISE summary of what you did plus the key findings and paths.
- Verify your work before reporting done. No preamble, no filler."""

# CC agent files name tools capitalized (Read, Grep); minicc uses lowercase. Map
# the common ones so a CC-authored agent file drops in; an unknown name is just
# lowercased (and won't match, like skills' allowed-tools caveat).
_TOOL_ALIASES = {
    "read": "read_file", "write": "write_file", "edit": "edit_file",
    "webfetch": "web_fetch", "websearch": "web_search",
}


@dataclass
class AgentDef:
    name: str
    description: str
    prompt: str                       # system prompt (the body)
    tools: list[str] | None = None    # allowlist of minicc tool names; None = all
    model: str | None = None          # None = inherit the session model


def _norm_tools(raw) -> list[str] | None:
    if not raw:
        return None
    names = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    out = [_TOOL_ALIASES.get(n.strip().lower(), n.strip().lower())
           for n in names if n.strip()]
    return out or None


def _roots():
    cwd = Path.cwd()
    for d in [*reversed(cwd.parents), cwd]:   # project: cwd + ancestors
        yield d / ".minicc" / "agents"
    yield Path.home() / ".minicc" / "agents"  # personal LAST — wins a clash


def agent_md_paths():
    """Every agent .md on disk — the reminder's watch-snapshot surface."""
    for root in _roots():
        if root.is_dir():
            yield from sorted(root.glob("*.md"))


def discover() -> dict[str, AgentDef]:
    """Parse every .minicc/agents/*.md (flat files; identity from `name` or the
    file stem). Personal overrides project on a name clash."""
    found: dict[str, AgentDef] = {}
    for md in agent_md_paths():
        try:
            meta, body = skills.parse_frontmatter(md.read_text())
        except OSError:
            continue
        name = str(meta.get("name") or md.stem)
        model = meta.get("model")
        model = None if (not model or model == "inherit") else str(model)
        found[name] = AgentDef(
            name, str(meta.get("description") or "").strip(),
            body.strip(), _norm_tools(meta.get("tools")), model,
        )
    return found


BUILTINS = {
    "general-purpose": AgentDef(
        "general-purpose",
        "Complex, multi-step tasks needing both exploration and edits. All tools; "
        "inherits the session model.",
        _GENERAL_PROMPT, None, None,
    ),
    "explore": AgentDef(
        "explore",
        "Search and understand the codebase without making changes. Read-only "
        "tools; runs on a cheaper model.",
        _EXPLORE_PROMPT, ["read_file", "glob", "grep"], EXPLORE_MODEL,
    ),
}


def resolve(subagent_type: str | None) -> AgentDef | None:
    """A definition (if any) wins over a built-in of the same name (CC parity).
    Omitted subagent_type → general-purpose (CC's catch-all default). None when
    the name is unknown."""
    name = subagent_type or "general-purpose"
    return discover().get(name) or BUILTINS.get(name)


def types() -> dict[str, AgentDef]:
    """All available types (built-ins + defined; a definition overrides)."""
    return {**BUILTINS, **discover()}


def listing_text() -> str:
    """The agent-types listing for the system-reminder (reminders.py)."""
    return "\n".join(f"- {a.name}: {a.description}" for a in types().values())
