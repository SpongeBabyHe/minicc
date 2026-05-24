from pathlib import Path

SCHEMA = {
    "name": "write_file",
    "description": "Create a new file or fully OVERWRITE an existing one with the given content. Parent directories are created if missing. This is destructive — there is no append mode. Returns the number of bytes written, or an error string.",
    "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
        "required": ["path", "content"]
    }
}


def write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"
