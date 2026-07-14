import platform
import subprocess
from datetime import date
from pathlib import Path

_TEMPLATE = """You are a coding agent working in the user's project, from the terminal.

Language: reply in the user's most recent language; don't mix languages in one response.

Working style:
- Search before assuming: use glob/grep to locate code before concluding it isn't there.
- Prefer the dedicated file/search tools (glob, grep, read_file) over bash when one fits;
  batch independent reads as parallel tool calls in one turn.
- For a partial change use edit_file — never rewrite a whole file with write_file.
- Read a tool's error before retrying; don't repeat a call hoping it works.
- Plan long, multi-step tasks with todo_write; delegate read-many-files exploration to task.
- web_search finds current/external information (titles + URLs + snippets); to read
  a specific page in depth, follow up with web_fetch on its URL. Don't search for
  stable knowledge you already have.

Verify your work: after editing code, check it before you report done — the loop is
gather → act → VERIFY. Prefer rules-based checks (the project's tests, linter, or
type-checker via bash); if a change is meant to fix or add behavior, run the
relevant test and confirm it passes. If verification fails, fix the cause and
re-run before saying you're finished; if the project has no runnable check, say so
rather than assuming it works. Don't claim something works that you haven't verified.

Memory: the auto-memory index (MEMORY.md) is injected each session; read topic files
on demand with the memory tool. Record durable, reusable learnings as you work
(user preferences, decisions, project facts, fixes) — one fact per topic file, and
keep MEMORY.md a concise index. Don't record transient task state. Writes ask for approval.

Permissions: bash, write_file, edit_file, web_fetch, and memory writes need the
user's approval — except read-only bash commands (ls, cat, grep, git status/log/diff,
…), which run without a prompt. If a call returns "User declined to run X", don't retry it —
acknowledge, then propose an alternative or ask what they'd prefer.

Defaults:
- When you have enough to act, act — don't over-explain plans for simple, reversible work.
- For destructive or irreversible work with unclear scope, ask first.
- Finish with a brief summary of what changed; don't restate the request.
- Output is for a terminal: concise, code in code blocks, headers only when they help.

When unsure: about what the user wants — ask briefly; about whether something
works — try it on a small case; about breaking compatibility — ask.
"""


def build_system_prompt() -> str:
    """The static instruction prefix (cache layer 1). No env here — that lives in
    build_session_context (layer 3, volatile-last) so this block stays byte-stable
    across sessions and can be cached globally, the way Claude Code groups it."""
    return _TEMPLATE


def _git_snapshot() -> str:
    """Branch + dirty/clean + recent commits at session start, or "" if not a git
    repo. Recent commits mirror CC's env block — commit-message style and active
    files are convention signals the model gets for free at session start. Brief on
    purpose — the agent runs `git status` via bash for live detail when it matters."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if branch.returncode != 0:
            return ""  # not a git repo
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=2,
        )
        dirty = len([ln for ln in status.stdout.splitlines() if ln.strip()])
        state = f"{dirty} uncommitted change(s)" if dirty else "clean"
        snapshot = (
            f"- Git: branch {branch.stdout.strip()}, {state} "
            "(at session start; run `git status` for live state)"
        )
        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=2,
        )
        if log.returncode == 0 and log.stdout.strip():
            recent = "\n".join(f"  {ln}" for ln in log.stdout.strip().splitlines())
            snapshot += f"\n- Recent commits:\n{recent}"
        return snapshot
    except (OSError, subprocess.SubprocessError):
        return ""


def build_session_context() -> str:
    """Session context (cache prefix layer 3): environment fixed at session start,
    with the volatile bit (git status) LAST so the stable prefix above stays
    byte-identical. Captured once at startup / on /clear — mirrors CC's env block.
    """
    lines = [
        "# Session context",
        "",
        f"- Working directory: {Path.cwd()}",
        f"- Platform: {platform.system()} ({platform.machine()})",
        f"- Date: {date.today().isoformat()}",
    ]
    git = _git_snapshot()
    if git:
        lines += ["", git]  # volatile-last
    return "\n".join(lines)


def _read_claude_md(path: Path) -> str:
    """One CLAUDE.md, truncated to CC's limits: first 200 lines or 25KB,
    whichever comes first. "" if missing/empty."""
    if not path.exists():
        return ""
    try:
        text = path.read_text().strip()
    except OSError:
        return ""
    if not text:
        return ""

    MAX_BYTES = 25 * 1024
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        text = encoded[:MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
        text += f"\n\n[CLAUDE.md truncated at 25KB; was {len(encoded):,} bytes]"

    MAX_LINES = 200
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        text = "\n".join(lines[:MAX_LINES])
        text += f"\n\n[CLAUDE.md truncated at 200 lines.]"
    return text


def load_project_context() -> str:
    """Load CLAUDE.md from cwd AND every ancestor directory (cache prefix layer 2).

    CC's monorepo behavior: parent directories' CLAUDE.md files are pulled in
    automatically alongside the project's own (root conventions + subproject
    specifics), outermost first so the nearest file reads last and wins where
    they disagree. Truncation (200 lines / 25KB) applies per file. Returns ""
    if none found. Recorded divergences: no ~/.claude global CLAUDE.md
    (auto-memory covers that role), no CLAUDE.local.md, no on-demand
    child-directory loading.
    """
    cwd = Path.cwd()
    sections = []
    for d in [*reversed(cwd.parents), cwd]:  # outermost → cwd
        text = _read_claude_md(d / "CLAUDE.md")
        if text:
            label = "" if d == cwd else f" — from {d}"
            sections.append(f"# Project context (CLAUDE.md{label})\n\n{text}")
    return "\n\n".join(sections)
