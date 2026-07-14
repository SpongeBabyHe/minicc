import re
import subprocess
from pathlib import Path
import uuid

DEFAULT_MAX_OUTPUT = 30_000      # CC's default
PREVIEW_CHARS = 2_000             # how much to show inline when truncated
# CC's documented Bash limits (tools-reference): default 2 minutes, and the model
# "can request up to 10 minutes per command with the `timeout` parameter" (ms).
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

SCHEMA = {
    "name": "bash",
    "description": (
        "Run a shell command. Use this ONLY when no other tool fits — e.g. running "
        "scripts, git, package managers, or tests. IMPORTANT: avoid running `ls`, "
        "`find`, `cat`, `head`, `tail`, or `grep` through bash unless you have verified "
        "a dedicated tool cannot do the job — use `glob` to find files, `grep` to "
        "search contents, `read_file` to read, and `edit_file`/`write_file` to edit. "
        "Commands are killed after 120s by default; for "
        "a known-slow command (build, install, model download, full test suite) request "
        "more with `timeout` (milliseconds, up to 600000 = 10 min). Don't start "
        "long-running servers or watchers in the foreground — run them detached. Output "
        "is capped at 30,000 chars; longer output is saved to a file and you get the "
        "path plus a 2,000-char preview — read_file (with offset/limit) that path for "
        "the rest."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {
                "type": "integer",
                "description": (
                    "Optional timeout in milliseconds (default 120000, max 600000). "
                    "Use for known-slow commands like builds or downloads."
                ),
            },
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


def bash(command: str, timeout: int | None = None) -> str:
    if any(p.search(command) for p in _DANGEROUS):
        return "Error: command blocked by safety filter (matched a destructive pattern)"
    # Clamp the model-requested timeout to CC's bounds; ignore nonsense values.
    ms = DEFAULT_TIMEOUT_MS if not timeout or timeout <= 0 else min(timeout, MAX_TIMEOUT_MS)
    try:
        r = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=ms / 1000,
        )
        out = str(r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return (
            f"Error: command timed out after {ms // 1000}s and was killed. For a "
            f"known-slow command, retry with a larger `timeout` (up to {MAX_TIMEOUT_MS} ms). "
            f"For a long-running process (server, watcher), run it detached instead."
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
