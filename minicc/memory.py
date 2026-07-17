"""Auto-memory store: machine-local, per-repo — minicc's take on Claude Code's
auto memory (docs/MEMORY.md).

Layout mirrors CC: a store at `~/.minicc/projects/<repo-key>/memory/` (repo-key =
the git toplevel path with `/`→`-`, else the cwd), shared across worktrees of the
same repo, never committed. `MEMORY.md` is a **concise index** loaded into every
session (first 200 lines / 25 KB); **topic files** hold one fact each and are read
on demand. The model reads/writes via the `memory` tool over a `/memories` prefix
this module maps onto the real store, with path-traversal protection.
"""

import subprocess
from datetime import date
from pathlib import Path

INDEX_NAME = "MEMORY.md"
_INDEX_MAX_LINES = 200
_INDEX_MAX_BYTES = 25 * 1024
_FILE_MAX_BYTES = 64 * 1024        # per-file write cap (don't let one file balloon)
_VIEW_MAX_CHARS = 16_000           # match CC's view truncation
_PREFIX = "/memories"

# Session toggle (like CC's autoMemoryEnabled). Off → the index isn't injected and
# writes are refused; reads still work so `/memory` can browse. Session-scoped for now.
_enabled = True


def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def _repo_root() -> str:
    """The git toplevel, or the cwd when not in a repo (matches CC's fallback)."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path.cwd())


def store_dir() -> Path:
    """`~/.minicc/projects/<repo-key>/memory/` for the current repo (machine-local)."""
    key = _repo_root().replace("/", "-").lstrip("-")
    return Path.home() / ".minicc" / "projects" / key / "memory"


def _resolve(mem_path: str) -> Path:
    """Map a `/memories/...` path to a real file under store_dir(), rejecting any
    path that escapes the memory root (`../`, symlinks, absolute escapes)."""
    root = store_dir().resolve()
    rel = mem_path[len(_PREFIX):] if mem_path.startswith(_PREFIX) else mem_path
    target = (root / rel.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path {mem_path} escapes the memory root")
    return target


def load_index() -> str:
    """The MEMORY.md index text — first 200 lines / 25 KB, whichever comes
    first. "" if disabled or there's no memory yet. Injected by reminders.py as
    a "Contents of <path> (user's auto-memory, …)" section of the claudeMd
    system-reminder (CC's label and placement), so no header of its own here."""
    if not _enabled:
        return ""
    idx = store_dir() / INDEX_NAME
    if not idx.exists():
        return ""
    text = idx.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return ""
    enc = text.encode("utf-8")
    if len(enc) > _INDEX_MAX_BYTES:
        text = enc[:_INDEX_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
    lines = text.splitlines()
    if len(lines) > _INDEX_MAX_LINES:
        text = "\n".join(lines[:_INDEX_MAX_LINES])
    return text


# ─── tool operations (return CC-style strings; the model reads whatever we return) ──
def view(path: str, view_range=None) -> str:
    try:
        target = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"

    if path.rstrip("/") == _PREFIX or target.is_dir():
        if not target.exists():
            return f"(memory is empty — nothing stored under {_PREFIX} yet)"
        entries = sorted(p for p in target.rglob("*") if p.is_file())
        if not entries:
            return f"(memory is empty — nothing stored under {_PREFIX} yet)"
        lines = [f"Contents of {_PREFIX}:"]
        for p in entries:
            rel = p.relative_to(target if target.is_dir() else target.parent)
            lines.append(f"{p.stat().st_size}\t{_PREFIX}/{rel}")
        return "\n".join(lines)

    if not target.exists():
        return f"The path {path} does not exist. Please provide a valid path."
    content = target.read_text(encoding="utf-8", errors="ignore")
    body = content.splitlines()
    if view_range and len(view_range) == 2:
        start, end = view_range
        end = len(body) if end == -1 else end
        body = body[max(0, start - 1):end]
        offset = max(0, start - 1)
    else:
        offset = 0
    numbered = "\n".join(f"{i + offset + 1:6d}\t{ln}" for i, ln in enumerate(body))
    if len(numbered) > _VIEW_MAX_CHARS:
        numbered = numbered[:_VIEW_MAX_CHARS] + "\n… (truncated; use view_range)"
    return f"Content of {path}:\n{numbered}"


def create(path: str, file_text: str) -> str:
    if not _enabled:
        return "Auto-memory is disabled (/memory on to enable)."
    try:
        target = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"
    if len(file_text.encode("utf-8")) > _FILE_MAX_BYTES:
        return f"Error: refusing to write {path}: over the {_FILE_MAX_BYTES // 1024}KB per-file cap."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_text, encoding="utf-8")
    return f"File created successfully at: {path}"


def str_replace(path: str, old_str: str, new_str: str = "") -> str:
    if not _enabled:
        return "Auto-memory is disabled (/memory on to enable)."
    try:
        target = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"
    if not target.exists():
        return f"Error: The path {path} does not exist. Please provide a valid path."
    content = target.read_text(encoding="utf-8", errors="ignore")
    count = content.count(old_str)
    if count == 0:
        return f"No replacement was performed, old_str did not appear verbatim in {path}."
    if count > 1:
        return (
            f"No replacement was performed. old_str appears {count} times in {path}; "
            "it must be unique — add surrounding lines to make it match one location."
        )
    target.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
    return "The memory file has been edited."


def delete(path: str) -> str:
    """Delete a memory file (needed to merge duplicates / retire stale facts).
    The memory root itself can't be deleted (official memory-tool contract)."""
    if not _enabled:
        return "Auto-memory is disabled (/memory on to enable)."
    try:
        target = _resolve(path)
    except ValueError as e:
        return f"Error: {e}"
    if target == store_dir().resolve() or path.rstrip("/") == _PREFIX:
        return "Error: cannot delete the /memories root itself."
    if not target.exists():
        return f"Error: The path {path} does not exist"
    if target.is_dir():
        for child in sorted(target.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        target.rmdir()
    else:
        target.unlink()
    return f"Successfully deleted {path}"


# ─── consolidation (CC's "Auto Dream", run manually via /memory consolidate) ──
_CONSOLIDATOR_SYSTEM = (
    "You are a memory maintainer. Your ONLY job is to tidy an agent's memory store "
    "under /memories using the memory tool. Do not invent facts; when unsure, keep. "
    "Make targeted edits (str_replace) over wholesale rewrites."
)

_CONSOLIDATE_PROMPT = """Consolidate the memory store. Today is {today}.

1. `view /memories`, then read MEMORY.md and every topic file.
2. Apply, in this order:
   - MERGE duplicates: if two files cover the same fact/preference, combine into
     the richer file and delete the other.
   - RETIRE dated facts: if a memory describes work that is clearly done or a
     deadline that has passed, delete it — or fold its lasting takeaway into a
     durable memory first.
   - FIX time references: convert relative dates ("next week", "recently") to
     absolute ones, using today's date.
   - PRUNE dead pointers: index lines or [[links]] to files that no longer exist.
   - REWRITE MEMORY.md as a concise index: exactly one line per surviving topic
     file (`- [Title](file.md) — one-line hook`), under 200 lines.
3. Finish with a short summary: files merged / deleted / edited and why.

Writes ask the user for approval — that is expected; proceed."""


def consolidate() -> str:
    """One agent_loop pass over the store with the memory tool only (the model
    does the judging — policy stays in the model, mechanism here). Returns the
    model's summary of what changed."""
    from minicc.agent import agent_loop          # lazy: avoid an import cycle
    from minicc.tools import TOOLS

    mem_tools = [t for t in TOOLS if t["name"] == "memory"]
    msgs = [{"role": "user", "content": _CONSOLIDATE_PROMPT.format(today=date.today().isoformat())}]
    agent_loop(
        msgs,
        system=_CONSOLIDATOR_SYSTEM,
        stream=False,                # nested run; tool lines are shown indented
        tools=mem_tools,
        max_turns=15,                # a tidy pass, not an open-ended session
        indent="  ",
    )
    for m in reversed(msgs):         # the final assistant text = the change summary
        if m.get("role") == "assistant":
            for b in m["content"]:
                if getattr(b, "type", None) == "text":
                    return b.text
    return "(no summary produced)"
