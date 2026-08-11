# Hooks

User-configured shell commands that fire at points in the agent loop. Faithful to
[Claude Code's hooks](https://code.claude.com/docs/en/hooks), adapted to minicc's
surfaces and tool names. Implemented in `minicc/hooks.py`; wired in
`minicc/query_engine.py` (tool events + the Stop gate), `minicc/cli/app.py`
(prompt and session lifecycle), and `minicc/context_management/manager.py`
(compaction events).

## Why hooks exist

CLAUDE.md and the system prompt are **advisory** — the model may or may not follow
them. A hook is **deterministic**: it runs as code every time, so it can enforce an
invariant the model can't be trusted to. This is CC's own framing ("Use hooks for
actions that must happen every time with zero exceptions"), and it's the hard-gate
complement to the verify-work stance in the system prompt: the stance *asks* the
model to run tests after editing; a `PostToolUse` hook *makes* the linter run, and
a `Stop` hook can hold the turn open until a check passes.

Canonical uses: block writes to a protected path, run a formatter/linter after every
edit, inject required context on each prompt.

## What's wired (and what isn't)

CC exposes ~30 hook events. Most target infrastructure minicc doesn't have (agent
teams, worktrees, MCP elicitation, plugins, task objects, a permission dialog / auto
classifier, file/config watching). Per the project's YAGNI-for-absent-infra rule,
only the events with a real minicc surface are wired:

| Event | minicc surface | Matcher | Can do |
|---|---|---|---|
| **PreToolUse** | `agent._run_tool`, before the permission gate | tool name | block (`deny`), pre-approve (`allow`), force a prompt (`ask`), rewrite args (`updatedInput`), inject context |
| **PostToolUse** | `agent._run_tool`, after the handler runs | tool name | feed the model a note (`additionalContext` / `decision:block` + `reason`), replace the result (`updatedToolOutput`), warn the user (`systemMessage`) |
| **UserPromptSubmit** | `cli.main`, before the turn | — | reject the prompt (`block`), inject context for the turn; stdin has `prompt` |
| **PreCompact** | `context_management.manager.compact`, before summarizing | `manual` \| `auto` | block compaction; stdin has `trigger`, `custom_instructions` |
| **PostCompact** | `context_management.manager.compact`, after history is replaced | `manual` \| `auto` | notification; stdin has `trigger`, `compact_summary` |
| **SessionStart** | `cli` / `compact`, at lifecycle starts | `startup` \| `resume` \| `clear` \| `compact` | inject `additionalContext`; compact-sourced context joins the new working set; stdin has `source`, `model` |
| **SessionEnd** | `cli`, at exit and `/clear` | `clear` \| `prompt_input_exit` | no event-specific block control; stdin has `reason` |
| **Stop** | `agent_loop`, when a turn is about to end | — | block the stop — reason is fed back and the model keeps working; context without a block enters the conversation but the turn ends; stdin has `last_assistant_message`, `stop_hook_active`. Runaway guard: overridden after **8 consecutive blocks** |

The universal JSON field `continue:false` applies to every event and takes
precedence over the event-specific behavior in this table. It aborts the current
processing path; for `Stop`, that means ending the turn even if the hook also
returned `decision:"block"`.

All events with a real minicc surface are now wired.
Not applicable (no surface): PermissionRequest, PostToolBatch, Notification, Task\*,
Worktree\*, Elicitation\*, ConfigChange, FileChanged, SubagentStart/Stop, etc.

Only `type: "command"` hooks run. `http` / `mcp_tool` / `prompt` / `agent` hook types
target infrastructure minicc doesn't have.

## Configuration

Hooks can live in user (`~/.minicc/settings.json`), shared project
(`<cwd>/.minicc/settings.json`), and local project
(`<repo-root>/.minicc/settings.local.json`) settings. For each event, all source
groups are concatenated in that order and fire. This is CC's `settings.json` shape
verbatim, so a hook written for Claude Code drops in unchanged — **except matchers
use minicc's tool names**
(`bash`, `write_file`, `edit_file`, `read_file`, `glob`, `grep`, `memory`,
`web_fetch`, `task`, `todo_write`), not CC's `Bash`/`Edit`.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          { "type": "command", "command": "./.minicc/hooks/guard.sh", "timeout": 10 }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "edit_file|write_file",
        "hooks": [{ "type": "command", "command": "ruff check ." }]
      }
    ]
  },
  "disableAllHooks": false
}
```

**Matcher semantics** (CC-faithful): `"*"` / `""` / omitted → match all; a plain name
or a `|`/`,`-separated list → exact membership; anything else → an unanchored regex
(`mcp__.*`). A malformed regex matches nothing rather than raising.

Hook config is read once per session and cached; it reloads at startup and on
`/clear` (minicc has no config watcher — CC's `ConfigChange` is absent-infra). Edit
a settings file mid-session and `/clear` to pick it up.

## I/O contract

The wired events use CC's field names and control semantics. Recorded surface
differences remain in the final section.

**Input** — JSON on the hook's stdin. Common fields on every event: `session_id`,
`transcript_path`, `cwd`, `permission_mode` (`"default"`; minicc has one mode),
`hook_event_name`. Event-specific fields include:

- `tool_name` + `tool_input` + `tool_use_id` (`PreToolUse` / `PostToolUse`)
- `tool_response` (`PostToolUse`)
- `prompt` (`UserPromptSubmit`)
- `trigger` + `custom_instructions` (`PreCompact`)
- `trigger` + `compact_summary` (`PostCompact`)
- `source` + `model` (`SessionStart`)
- `last_assistant_message` + `stop_hook_active` (`Stop`)

**Output** — two channels:

- **Exit code.** `0` → parse JSON on stdout for decision control. Plain stdout
  becomes context for `SessionStart` and `UserPromptSubmit`; other events ignore
  it. `2` → **block**, and stderr is the reason fed back to the model. Any other
  non-zero → **non-blocking**; the first stderr line is shown to the user.
- **JSON stdout** (only on exit 0). Honored fields: universal `continue`
  (`false` stops processing before event-specific decisions) + `stopReason`,
  `systemMessage`, `decision` (`"block"`) + `reason`, and
  `hookSpecificOutput.{ permissionDecision (allow|deny|ask), permissionDecisionReason,
  additionalContext, updatedInput, updatedToolOutput }`.

`deny` wins: once any hook blocks a call, a later hook's `allow` can't override it.

## Example: block `rm -rf`, lint after edits

`.minicc/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "bash",
        "hooks": [{ "type": "command", "command": "./.minicc/hooks/no-rm.sh" }] }
    ],
    "PostToolUse": [
      { "matcher": "edit_file|write_file",
        "hooks": [{ "type": "command", "command": "ruff check . 2>&1 | head -5 >&2 || true" }] }
    ]
  }
}
```

`.minicc/hooks/no-rm.sh`:

```sh
#!/bin/sh
cmd=$(python3 -c 'import sys,json; print(json.load(sys.stdin)["tool_input"].get("command",""))')
case "$cmd" in
  *"rm -rf"*)
    echo "rm -rf is blocked by policy" >&2
    exit 2 ;;   # exit 2 → deny; stderr → the model
esac
exit 0
```

## Divergences from CC (recorded)

- **Event set** is a subset (see the table) — the rest are absent-infra, not skipped
  for effort.
- **Matcher namespace** is minicc's lowercase tool names, not CC's `Bash`/`Edit`.
- **Config path** is `.minicc/settings.json`, not `.claude/settings.json`.
- No config watcher: hooks reload at startup / `/clear`, not live.
- `PreToolUse` `additionalContext` is appended to the tool result;
  `UserPromptSubmit` `additionalContext` rides as an extra user message for the turn
  (CC injects it into the model's context the same way).
- `SessionStart` supports `compact`; `sessionTitle` / `initialUserMessage` /
  `watchPaths` / `reloadSkills` remain absent-infra.
- **SessionEnd** reasons are the two with a surface (`clear`, `prompt_input_exit`);
  `logout` / `resume` / `bypass_permissions_disabled` have none.
- A hook that persistently blocks auto-compaction does not increment the thrash
  guard: a veto is a user decision, not a failed compaction. If the resulting API
  request is structurally too large, the original 400/413 surfaces.
- **Stop** fires for the main session only; sub-agent turn-end is CC's separate
  SubagentStop event (not wired — minicc's `task` sub-agents return a summary to the
  parent, who verifies). The 8-block cap comes from CC's best-practices page ("Claude
  Code overrides the hook and ends the turn after 8 consecutive blocks"); the hooks
  reference documents `stop_hook_active`. minicc sends the field and enforces the
  documented cap.
