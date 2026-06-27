import re
import subprocess
from pathlib import Path
import uuid

DEFAULT_MAX_OUTPUT = 30_000      # CC's default
PREVIEW_CHARS = 2_000             # how much to show inline when truncated
TIMEOUT_SECONDS = 120

SCHEMA = {
    "name": "bash",
    "description": (
        "Run a shell command. Use this ONLY when no other tool fits — e.g. running "
        "scripts, git, package managers, or tests. For finding files prefer `glob`; for "
        "searching contents prefer `grep`; for reading prefer `read_file`; for editing "
        "prefer `edit_file`/`write_file`. Commands are killed after 120s, so don't start "
        "long-running servers or watchers in the foreground. Output is capped at 30,000 "
        "chars; longer output is saved to a file and you get the path plus a 2,000-char "
        "preview — read_file (with offset/limit) that path for the rest."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."}
        },
        "required": ["command"],
    },
}

# Coarse defense-in-depth net (bash is also user-gated). Word/anchor-based so it
# catches the catastrophic forms WITHOUT false-positiving on legitimate commands
# like `rm -rf /tmp/x`, `2>/dev/null`, or `echo pseudocode`.
_DANGEROUS = [
    re.compile(r"\brm\s+-[rfRF]+\s+/(\s|$|\*)"),   # rm -rf / (root) — not /tmp/...
    re.compile(r"\bsudo\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r">\s*/dev/(sd|hd|nvme|disk)"),     # overwrite a raw disk (allows /dev/null)
    re.compile(r"\bmkfs(\.\w+)?\b"),
]


def _ensure_output_dir() -> Path:
    """Return .minicc/bash_outputs/ in the current project, creating it if needed.

    Also writes a self-ignoring .gitignore inside .minicc/ so even if the project
    doesn't have .minicc/ in its own .gitignore, contents stay untracked.
    """
    out_dir = Path.cwd() / ".minicc" / "bash_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    gitignore = out_dir.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    return out_dir


def bash(command: str) -> str:
    if any(p.search(command) for p in _DANGEROUS):
        return "Error: command blocked by safety filter (matched a destructive pattern)"
    try:
        r = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )
        out = str(r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return (
            f"Error: command timed out after {TIMEOUT_SECONDS}s and was killed. For a "
            f"long-running process (server, watcher), run it in the background or bound "
            f"its runtime."
        )
    except Exception as e:
        return f"Error: {e}"

    if len(out) <= DEFAULT_MAX_OUTPUT:
        return out

    # Output too large — save full to disk, return path + preview
    out_dir = _ensure_output_dir()
    file_id = uuid.uuid4().hex[:8]
    path = out_dir / f"{file_id}.txt"
    path.write_text(out)

    preview = out[:PREVIEW_CHARS]
    return (
        f"[Output truncated: {len(out):,} chars total, showing first {PREVIEW_CHARS}]\n"
        f"[Full output saved to: {path}]\n"
        f"[Use read_file on the path above (with offset/limit) to see the rest]\n\n"
        f"--- Preview ---\n{preview}"
    )
