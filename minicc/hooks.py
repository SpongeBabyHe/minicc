"""Event hooks — user-configured shell commands that fire at points in the agent
loop. Faithful to Claude Code's hooks, adapted to minicc's surfaces + tool names.

WHY hooks exist (CC's framing): CLAUDE.md and the system prompt are *advisory* — the
model may or may not follow them. A hook is *deterministic*: it runs as code every
time, so it can enforce an invariant the model can't be trusted to (block writes to a
protected path, run the linter after every edit, inject required context on each
prompt). It's the hard-gate complement to the verify-work stance.

Config lives in .minicc/settings.json (global + project, merged) under "hooks",
using CC's exact schema so a Claude Code hook drops in unchanged:

    {
      "hooks": {
        "PreToolUse": [
          {"matcher": "bash",
           "hooks": [{"type": "command", "command": "…/guard.sh", "timeout": 10}]}
        ]
      },
      "disableAllHooks": false
    }

Scope (YAGNI for absent infra): only CC events with a real minicc surface are wired —
PreToolUse, PostToolUse, UserPromptSubmit (tool/prompt), PreCompact, PostCompact,
SessionStart, SessionEnd (lifecycle). Only `type: "command"` runs; http/mcp_tool/
prompt/agent target infrastructure minicc doesn't have. Matchers use minicc's tool
names (bash, write_file, edit_file), not CC's Bash/Edit. See HOOKS.md.

I/O contract (verbatim-faithful to CC):
- Input: JSON on stdin — common fields (session_id, transcript_path, cwd,
  hook_event_name) + event-specific (tool_name, tool_input, tool_response,
  user_prompt).
- Output: exit 0 → parse JSON stdout for decision control (plain stdout is ignored,
  goes to a debug log in CC); exit 2 → block, stderr is the reason fed to the model;
  any other non-zero → non-blocking, stderr's first line shown to the user.
- JSON stdout honored: continue, stopReason, systemMessage, decision ("block"),
  reason, hookSpecificOutput.{permissionDecision (allow|deny|ask),
  permissionDecisionReason, additionalContext, updatedInput, updatedToolOutput}.

The module is mechanism-only: `run` returns a normalized Decision; each CALL SITE
interprets `block` per its event (deny the tool / reject the prompt / feed back), the
way CC's own hook system separates the runner from the event semantics.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from minicc import config, sessions, ux

_TIMEOUT_DEFAULT = 60  # seconds; a hook entry's "timeout" (seconds) overrides

# Merged config is read once and cached — settings don't change mid-session (minicc
# has no config watcher, unlike CC's ConfigChange event; YAGNI). reset() reloads it,
# called at startup and on /clear.
_CACHE: tuple[dict, bool] | None = None


def reset() -> None:
    """Drop the cached hook config so the next run() re-reads settings.json."""
    global _CACHE
    _CACHE = None


def _load() -> tuple[dict, bool]:
    global _CACHE
    if _CACHE is None:
        _CACHE = config.load_hooks()
    return _CACHE


@dataclass
class Decision:
    """Normalized outcome of the hooks for one event. Sticky where it must be: once a
    hook blocks, a later hook can't un-block it (CC: deny wins)."""

    block: bool = False  # a hook said "block" (meaning is per-event)
    allow: bool = False  # PreToolUse "allow": bypass the permission gate
    ask: bool = False  # PreToolUse "ask": force the permission prompt
    reason: str | None = None  # why blocked / fed back to the model
    stop: bool = False  # continue:false — halt the whole turn
    stop_reason: str | None = None
    updated_input: dict | None = None  # PreToolUse: rewrite the tool arguments
    updated_output: str | None = None  # PostToolUse: replace the tool result text
    system_messages: list = field(default_factory=list)  # shown to the user
    additional_context: list = field(default_factory=list)  # injected for the model


def _match(matcher, value: str) -> bool:
    """CC matcher semantics: "*"/""/None → match all; a plain name or a |/,-separated
    list → exact membership; anything else → an unanchored regex (JS-unanchored ≈
    Python re.search). A malformed regex matches nothing rather than raising."""
    if matcher in (None, "", "*"):
        return True
    if re.fullmatch(r"[A-Za-z0-9_\-, |]+", matcher):
        names = [p.strip() for p in re.split(r"[|,]", matcher) if p.strip()]
        return value in names
    try:
        return re.search(matcher, value) is not None
    except re.error:
        return False


def _common(event: str, session_id: str | None) -> dict:
    tp = sessions.path(session_id)
    return {
        "session_id": session_id or "",
        "transcript_path": str(tp) if tp else "",
        "cwd": str(Path.cwd()),
        "hook_event_name": event,
    }


def run(
    event: str, session_id: str | None = None, match_value: str = "", **payload
) -> Decision:
    """Fire every configured command hook for `event` whose matcher accepts
    `match_value` (the tool name for tool events; "" for events without matchers).
    `payload` is merged into the JSON sent on the hook's stdin. Returns a Decision the
    caller interprets. No hooks / disabled → an empty (no-op) Decision."""
    events, disabled = _load()
    decision = Decision()
    if disabled:
        return decision
    groups = events.get(event)
    if not groups:
        return decision
    stdin_obj = {**_common(event, session_id), **payload}
    for group in groups:
        if not isinstance(group, dict):
            continue
        if not _match(group.get("matcher"), match_value):
            continue
        for entry in group.get("hooks", []):
            if isinstance(entry, dict) and entry.get("type", "command") == "command":
                _run_one(entry, stdin_obj, decision)
    return decision


def _run_one(entry: dict, stdin_obj: dict, decision: Decision) -> None:
    cmd = entry.get("command")
    if not cmd:
        return
    timeout = entry.get("timeout", _TIMEOUT_DEFAULT)
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            input=json.dumps(stdin_obj),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        decision.system_messages.append(
            f"hook timed out after {timeout}s: {ux.truncate(str(cmd), 80)}"
        )
        return
    except OSError as e:
        decision.system_messages.append(f"hook failed to run: {e}")
        return
    _apply(proc, decision)


def _apply(proc, decision: Decision) -> None:
    code = proc.returncode
    if code == 2:
        # blocking error: stderr is the reason fed back to the model
        decision.block = True
        if proc.stderr.strip():
            decision.reason = proc.stderr.strip()
        return
    if code != 0:
        # non-blocking error: first stderr line surfaced to the user, action continues
        line = next((ln for ln in proc.stderr.splitlines() if ln.strip()), "")
        if line:
            decision.system_messages.append(line.strip())
        return
    # exit 0: JSON stdout is the decision-control channel (plain stdout is ignored)
    out = proc.stdout.strip()
    if not out:
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return
    if isinstance(data, dict):
        _apply_json(data, decision)


def _apply_json(data: dict, decision: Decision) -> None:
    if data.get("continue") is False:
        decision.stop = True
        decision.stop_reason = data.get("stopReason")
    if data.get("systemMessage"):
        decision.system_messages.append(str(data["systemMessage"]))
    # generic block (PostToolUse / UserPromptSubmit / Stop)
    if data.get("decision") == "block":
        decision.block = True
        if data.get("reason"):
            decision.reason = str(data["reason"])

    hso = data.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return
    if hso.get("additionalContext"):
        decision.additional_context.append(str(hso["additionalContext"]))
    if isinstance(hso.get("updatedInput"), dict):
        decision.updated_input = hso["updatedInput"]
    uo = hso.get("updatedToolOutput")
    if uo is not None:
        decision.updated_output = uo.get("text") if isinstance(uo, dict) else str(uo)
    # PreToolUse permission decision (deny wins — never un-block)
    pd = hso.get("permissionDecision")
    if pd == "deny":
        decision.block = True
        decision.reason = hso.get("permissionDecisionReason") or decision.reason
    elif pd == "ask":
        decision.ask = True
    elif pd == "allow":
        decision.allow = True
