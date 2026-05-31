import subprocess
from pathlib import Path
import uuid

DEFAULT_MAX_OUTPUT = 30_000      # CC's default
PREVIEW_CHARS = 2_000             # how much to show inline when truncated

SCHEMA = {
    "name": "bash",
    "description": "Run an arbitrary shell command. Use this ONLY when no other tool fits — e.g., running scripts, git operations, package managers. For finding files, prefer `glob`. For searching content, prefer `grep`. For reading files, prefer `read_file`. For editing, prefer `edit_file` or `write_file`. Output is capped at 30,000 characters; longer outputs are saved to disk and you receive the file path plus a 2000-char preview — read_file on that path for the rest.",
    "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
        "required": ["command"]
    }
}


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
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=120,

        )
        out = str(r.stdout + r.stderr).strip()
    except Exception as e:
        return f" Error: {e}"

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
