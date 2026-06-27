from pathlib import Path

SCHEMA = {
    "name": "write_file",
    "description": "Create a new file or fully OVERWRITE an existing one with the given content. Parent directories are created if missing. This is destructive — there is no append mode, and for a partial change you should use edit_file instead. Returns the number of bytes written, or an error string.",
    "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write."},
                "content": {"type": "string", "description": "Full contents to write (replaces any existing file)."}
            },
        "required": ["path", "content"]
    }
}


def write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        n = p.write_text(content)  # returns chars written
        n_bytes = len(content.encode("utf-8"))
        return f"Wrote {n_bytes} bytes ({n} chars) to {path}"
    except Exception as e:
        return f"Error: {e}"
