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
    dangerous = []
    for d in dangerous:
        if d in command:
            return f"Dangerous command."
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
