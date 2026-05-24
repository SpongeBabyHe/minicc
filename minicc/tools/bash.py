import subprocess

SCHEMA = {
    "name": "bash",
    "description": "Run a shell command.",
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
