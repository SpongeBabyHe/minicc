import subprocess

SCHEMA = {
    "name": "bash",
    "description": "Run an arbitrary shell command. Use this ONLY when no other tool fits — e.g., running scripts, git operations, package managers. For finding files, prefer `glob`. For searching content, prefer `grep`. For reading files, prefer `read_file`. For editing, prefer `edit_file` or `write_file`. Returns combined stdout+stderr, truncated.",
    "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"}
            },
        "required": ["command"]
    }
}


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
        return out
    except Exception as e:
        return f" Error: {e}"
