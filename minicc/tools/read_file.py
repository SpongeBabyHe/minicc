from pathlib import Path

SCHEMA = {
    "name": "read_file",
    "description": "Read a file from disk and return its contents. Use this when you need to inspect existing code, configs, or docs. Use 'limit' to cap the number of lines returned for large files. Returns an error string starting with 'Error:' on failure (file not found, permission denied, binary file, etc.).",
    "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"}
            },
        "required": ["path"]
    }
}


def read_file(path: str, limit: int = None) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Error: {path} does not exist"
    if not p.is_file():
        return f"Error: {path} is not a file"
    try:
        text = p.read_text()
        lines = text.splitlines()

        # if limit is provided and limit is less than the number of lines, truncate the lines
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]

        # concatenate the lines and return the first 50000 characters
        return "\n".join(lines)[:50000]

    except Exception as e:
        return f"Error: {e}"
