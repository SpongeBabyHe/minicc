from pathlib import Path

# keep current truncated setting, should redesign with token cost
MAX_OUTPUT_CHARS = 50_000

SCHEMA = {
    "name": "read_file",
    "description": (
        "Read a UTF-8 text file and return its contents. Use `offset` (1-based start line) "
        "and `limit` (number of lines) to read just a window of a large file. Output is "
        "capped at 50,000 chars; longer output is truncated with a notice. Returns an "
        "error string starting with 'Error:' on failure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read."},
            "offset": {
                "type": "integer",
                "description": "1-based line number to start reading from (default 1).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to return.",
            },
        },
        "required": ["path"],
    },
}


def read_file(path: str, offset: int = None, limit: int = None) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Error: {path} does not exist"
    if not p.is_file():
        return f"Error: {path} is not a file"
    try:
        text = p.read_text()
    except UnicodeDecodeError:
        return f"Error: {path} is not a UTF-8 text file"
    except Exception as e:
        return f"Error: {e}"

    lines = text.splitlines()
    total_lines = len(lines)

    # offset: 1-based start line; skip everything before it.
    start = (offset - 1) if (offset and offset > 0) else 0
    window = lines[start:]

    line_truncated = limit is not None and limit < len(window)
    if line_truncated:
        window = window[:limit]
    body = "\n".join(window)

    # Char cap as final safety
    char_truncated = len(body) > MAX_OUTPUT_CHARS
    if char_truncated:
        body = body[:MAX_OUTPUT_CHARS]

    # Tell the model what window it actually got, so it can ask for more.
    if line_truncated or char_truncated or start > 0:
        last = start + len(window)
        note = f"Showing lines {start + 1}-{last} of {total_lines}"
        if char_truncated:
            note += f"; truncated at {MAX_OUTPUT_CHARS} chars"
        body += f"\n\n[{note} — pass offset/limit to see more.]"

    return body
