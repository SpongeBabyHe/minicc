from pathlib import Path

# keep current truncated setting, should redesign with token cost
MAX_OUTPUT_CHARS = 50_000

SCHEMA = {
    "name": "read_file",
    "description": "Read a UTF-8 text file and return its contents. Use 'limit' to cap the number of lines for large files. Output is capped at 50,000 chars; longer files are truncated with a notice. Returns an error string starting with 'Error:' on failure.",
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
    except UnicodeDecodeError:
        return f"Error: {path} is not a UTF-8 text file"
    except Exception as e:
        return f"Error: {e}"

    lines = text.splitlines()
    total_lines = len(lines)

    # if limit is provided and limit is less than the number of lines, truncate the lines
    if limit and limit < len(lines):
        lines = lines[:limit]
        line_truncated = True
    else:
        line_truncated = False
    body = "\n".join(lines)

    # Char cap as final safety
    char_truncated = len(body) > MAX_OUTPUT_CHARS
    if char_truncated:
        body = body[:MAX_OUTPUT_CHARS]

    # Tell the model what was truncated
    if line_truncated:
        body += f"\n\n[Showing first {limit} of {total_lines} lines — pass a higher limit to see more.]"
    elif char_truncated:
        body += f"\n\n[Truncated at {MAX_OUTPUT_CHARS} chars; file is {len(text):,} chars total.]"

    return body
