# minicc

A tiny coding agent CLI — a from-scratch reimplementation of Claude Code's
core loop, built as a learning project.

## What it does

minicc is a terminal REPL that lets you ask Claude to read, search, and edit
code in your project. It runs an agent loop with these tools:

- `bash` — run shell commands (with permission prompt)
- `read_file` / `write_file` / `edit_file` — file I/O
- `glob` / `grep` — search by name or content

Destructive operations (`bash`, `write_file`, `edit_file`) require explicit
approval — you can approve once, deny, or grant "all" for the session.

## Run

```bash
cp .env.example .env       # set ANTHROPIC_API_KEY and MODEL_ID
pip install -e .
python -m minicc.cli
```