# minicc

A tiny coding agent CLI — a from-scratch reimplementation of Claude Code's
core loop, built as a learning project.

## What it does

minicc is a terminal REPL that lets you ask Claude to read, search, and edit
code in your project. It runs an agent loop with these tools:

- `bash` — run shell commands (120s default timeout, model-extendable to 600s)
- `read_file` / `write_file` / `edit_file` — file I/O
- `glob` / `grep` — search by name or content
- `task` — delegate exploration to a read-only subagent (own context, cheaper model)
- `todo_write` — a model-maintained task list for long work
- `memory` — cross-session auto-memory (index + topic files)
- `web_fetch` / `web_search` — read a URL (url+prompt extraction) / search the web

Gated operations (`bash`, `write_file`, `edit_file`, `web_fetch`, memory
writes) require explicit approval — approve once, deny, or grant "all" for the
session. Reads are never gated. See `PERMISSIONS.md`.

Beyond the loop: prompt caching + two-band context management (eviction →
compaction; `CONTEXT_MANAGEMENT.md`), session persistence with
`--continue`/`--resume` (`SESSIONS.md`), file checkpoints + `/rewind`
(`CHECKPOINT.md`), streaming markdown output (`STREAMING.md`), and
Claude Code-compatible hooks (`HOOKS.md`).

## Run

```bash
cp .env.example .env       # set ANTHROPIC_API_KEY (model lives in settings, not env)
pip install -e .
python -m minicc.cli
```