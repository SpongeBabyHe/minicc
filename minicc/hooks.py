"""Event hooks — user-configured shell commands that fire at points in the agent
loop. Faithful to Claude Code's hooks, adapted to minicc's surfaces + tool names.

WHY hooks exist (CC's framing): CLAUDE.md and the system prompt are *advisory* — the
model may or may not follow them. A hook is *deterministic*: it runs as code every
time, so it can enforce an invariant the model can't be trusted to (block writes to a
protected path, run the linter after every edit, inject required context on each
prompt). It's the hard-gate complement to the verify-work stance.

Config lives in user, project, and local settings under "hooks". Groups are
concatenated in that source order, using CC's schema so a hook drops in unchanged:

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

I/O contract:
- Input: JSON on stdin — common fields (session_id, transcript_path, cwd,
  permission_mode, hook_event_name) + event-specific (tool_name, tool_input,
  tool_use_id, tool_response,
  prompt, trigger, custom_instructions, compact_summary, source).
- Output: exit 0 → parse JSON stdout for decision control; plain stdout becomes
  context for SessionStart/UserPromptSubmit and is ignored for other events.
  Exit 2 is event-specific: blockable events stop with stderr as feedback;
  SessionStart/SessionEnd/PostCompact continue and show stderr to the user.
  Any other non-zero is non-blocking and surfaces stderr's first line.
- JSON stdout honored: continue, stopReason, systemMessage, decision ("block"),
  reason, hookSpecificOutput.{permissionDecision (allow|deny|ask),
  permissionDecisionReason, additionalContext, updatedInput, updatedToolOutput}.
  Output strings above 10,000 characters are spilled under
  .minicc/hook_outputs/ and replaced by a bounded preview plus path.

The module is mechanism-only: `run` returns a normalized Decision; each CALL SITE
interprets `block` per its event. `continue:false` is universal and takes precedence
over event-specific decisions.
"""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from minicc import config, sessions, ux

_TIMEOUT_DEFAULT = 60  # seconds; a hook entry's "timeout" (seconds) overrides
_OUTPUT_CHAR_LIMIT = 10_000
_EXIT_2_USER_ONLY_EVENTS = frozenset(
    {
        "Notification",
        "PostCompact",
        "SessionEnd",
        "SessionStart",
        "Setup",
        "SubagentStart",
    }
)

# Merged config is read once and cached — settings don't change mid-session (minicc
# has no config watcher, unlike CC's ConfigChange event; YAGNI). reset() reloads it,
# called at startup and on /clear.
_CACHE: tuple[dict, bool] | None = None


def reset() -> None:
    """Drop the cached hook config so the next run() re-reads settings.json."""
    global _CACHE
    _CACHE = None


def _load() -> tuple[dict, bool]:
    """Return cached merged hook settings, loading them on first use."""
    global _CACHE
    if _CACHE is None:
        _CACHE = config.load_hooks()
    return _CACHE


@dataclass
class Decision:
    """Normalized outcome of ALL hooks that matched one event.

    This is deliberately a UNION of every event's possible outcome: `run` is
    mechanism-only and doesn't know what the fields mean, so each CALL SITE picks
    the ones its event defines. Read a field against the matrix below — in
    isolation the type says nothing about what is meaningful when.

    | event            | consumer                    | fields it reads                                    |
    |------------------|-----------------------------|----------------------------------------------------|
    | PreToolUse       | query_engine._run_tool      | block · allow · ask · reason · updated_input · ctx |
    | PostToolUse      | query_engine._post_tool     | block+reason · updated_output · ctx                 |
    | Stop             | query_engine._stop_gate     | block · reason · ctx                                |
    | UserPromptSubmit | cli.main                    | block · reason · ctx                                |
    | PreCompact       | context_management.compact  | block · reason                                      |
    | PostCompact      | context_management.compact  | notification                                        |
    | SessionStart     | cli / compact               | ctx                                                 |
    | SessionEnd       | cli._fire_session_end       | notification                                        |

    `system_messages` is the one field EVERY site reads (surfaced via `surface`).
    `additional_context` ("ctx" above) is injected five different ways: appended
    to the tool result (PreToolUse), added as notes (PostToolUse), entered into
    the conversation (Stop), added as an extra user message (UserPromptSubmit),
    or folded into the session-context layer (SessionStart).

    ACCUMULATION across the hooks of one event (`run` threads one instance
    through all of them):
      - lists (`system_messages`, `additional_context`) APPEND;
      - booleans (`block`, `allow`, `stop`) LATCH True and never fall back;
      - scalars (`reason`, `updated_input`, `updated_output`) are
        LAST-WRITER-WINS: with two hooks rewriting the same tool input, the
        earlier rewrite is silently dropped.
    """

    block: bool = False  # a hook said "block" (meaning is per-event)
    allow: bool = False  # PreToolUse "allow": bypass the permission gate
    ask: bool = False  # PreToolUse "ask": force the permission prompt
    reason: str | None = None  # why blocked / fed back to the model
    stop: bool = False  # continue:false — stop processing before event decisions
    stop_reason: str | None = None
    updated_input: dict | None = None  # PreToolUse: rewrite the tool arguments
    updated_output: str | None = None  # PostToolUse: replace the tool result text
    system_messages: list = field(default_factory=list)  # shown to the user
    additional_context: list = field(default_factory=list)  # injected for the model


class HookStop(RuntimeError):
    """Control-flow signal for universal hook output `continue:false`."""

    def __init__(self, event: str, reason: str | None = None):
        self.event = event
        self.reason = reason or "no reason given"
        super().__init__(f"{event} hook stopped processing: {self.reason}")


def surface(decision: Decision, indent: str = "") -> None:
    """Show a Decision's `system_messages` to the user — the one field every call
    site consumes, identically. Kept here so the "[hook] …" presentation has a
    single home instead of eight copies of the same loop."""
    for m in decision.system_messages:
        ux.say(f"{indent}[hook] {m}", style=ux.S_INFO)


def raise_if_stopped(decision: Decision, event: str) -> None:
    """Apply the universal continue:false field before event-specific output."""
    if decision.stop:
        raise HookStop(event, decision.stop_reason)


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
    """Build the fields present in every command-hook stdin payload."""
    tp = sessions.path(session_id)
    return {
        "session_id": session_id or "",
        "transcript_path": str(tp) if tp else "",
        "cwd": str(Path.cwd()),
        "permission_mode": "default",
        "hook_event_name": event,
    }


def _bounded_output(
    value,
    *,
    event: str,
    session_id: str | None,
) -> str:
    """Cap one hook-output string and spill its complete value when oversized."""
    text = str(value)
    if len(text) <= _OUTPUT_CHAR_LIMIT:
        return text

    safe_event = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", event)[:48]
        or "hook"
    )
    safe_session = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id or "")[:48]
        or "no-session"
    )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    try:
        output_dir = config.ensure_project_dir("hook_outputs") / safe_session
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{safe_event}-{digest}.txt"
        output_path.write_text(text, encoding="utf-8")
        suffix = (
            f"\n...[+{len(text):,} total chars; hook output capped at "
            f"{_OUTPUT_CHAR_LIMIT:,}]\n"
            f"Full hook output saved to: {output_path}"
        )
    except OSError:
        suffix = (
            f"\n...[+{len(text):,} total chars; hook output capped at "
            f"{_OUTPUT_CHAR_LIMIT:,}; full output could not be saved]"
        )
    preview_chars = max(0, _OUTPUT_CHAR_LIMIT - len(suffix))
    return text[:preview_chars] + suffix


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
                _run_one(entry, stdin_obj, decision, event)
    return decision


def _run_one(
    entry: dict, stdin_obj: dict, decision: Decision, event: str
) -> None:
    """Execute one configured command hook and merge its normalized outcome."""
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
    _apply(
        proc,
        decision,
        event,
        session_id=stdin_obj.get("session_id") or None,
    )


def _apply(
    proc,
    decision: Decision,
    event: str,
    *,
    session_id: str | None = None,
) -> None:
    """Merge one command result using the event-specific exit-code contract."""
    code = proc.returncode
    if code == 2:
        stderr = _bounded_output(
            proc.stderr.strip(),
            event=event,
            session_id=session_id,
        )
        if event in _EXIT_2_USER_ONLY_EVENTS:
            if stderr:
                decision.system_messages.append(stderr)
            return
        # Blocking event: stderr is the reason fed back to the model.
        decision.block = True
        if stderr:
            decision.reason = stderr
        return
    if code != 0:
        # non-blocking error: first stderr line surfaced to the user, action continues
        line = next((ln for ln in proc.stderr.splitlines() if ln.strip()), "")
        if line:
            decision.system_messages.append(
                _bounded_output(
                    line.strip(),
                    event=event,
                    session_id=session_id,
                )
            )
        return
    # exit 0: JSON is the decision-control channel. Two lifecycle/prompt events
    # define plain stdout as context instead.
    out = proc.stdout.strip()
    if not out:
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        if event in ("SessionStart", "UserPromptSubmit"):
            decision.additional_context.append(
                _bounded_output(
                    out,
                    event=event,
                    session_id=session_id,
                )
            )
        return
    if isinstance(data, dict):
        _apply_json(
            data,
            decision,
            event=event,
            session_id=session_id,
        )


def _apply_json(
    data: dict,
    decision: Decision,
    *,
    event: str,
    session_id: str | None,
) -> None:
    """Merge supported structured-output fields into an aggregate decision."""
    if data.get("continue") is False:
        decision.stop = True
        if data.get("stopReason") is not None:
            decision.stop_reason = _bounded_output(
                data["stopReason"],
                event=event,
                session_id=session_id,
            )
    if data.get("systemMessage"):
        decision.system_messages.append(
            _bounded_output(
                data["systemMessage"],
                event=event,
                session_id=session_id,
            )
        )
    # generic block (PostToolUse / UserPromptSubmit / Stop)
    if data.get("decision") == "block":
        decision.block = True
        if data.get("reason"):
            decision.reason = _bounded_output(
                data["reason"],
                event=event,
                session_id=session_id,
            )

    hso = data.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return
    if hso.get("additionalContext"):
        decision.additional_context.append(
            _bounded_output(
                hso["additionalContext"],
                event=event,
                session_id=session_id,
            )
        )
    if isinstance(hso.get("updatedInput"), dict):
        decision.updated_input = hso["updatedInput"]
    uo = hso.get("updatedToolOutput")
    if uo is not None:
        updated_output = uo.get("text") if isinstance(uo, dict) else uo
        if updated_output is not None:
            decision.updated_output = _bounded_output(
                updated_output,
                event=event,
                session_id=session_id,
            )
    # PreToolUse permission decision (deny wins — never un-block)
    pd = hso.get("permissionDecision")
    if pd == "deny":
        decision.block = True
        permission_reason = hso.get("permissionDecisionReason")
        if permission_reason:
            decision.reason = _bounded_output(
                permission_reason,
                event=event,
                session_id=session_id,
            )
    elif pd == "ask":
        decision.ask = True
    elif pd == "allow":
        decision.allow = True
