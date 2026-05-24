from pathlib import Path

SCHEMA = {
    "name": "edit_file",
    "description": "Replace one occurrence of old_text with new_text in a file. old_text must match EXACTLY (including whitespace and indentation) and must appear EXACTLY ONCE in the file — if it appears zero or multiple times, this returns an error and you must provide more surrounding context to disambiguate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"]
    }
}


def edit_file(path: str, old_text: str, new_text: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Error: {path} does not exist"
    if not p.is_file():
        return f"Error: {path} is not a file"
    try:
        content = p.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        p.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"
