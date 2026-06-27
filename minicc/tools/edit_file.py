from pathlib import Path

SCHEMA = {
    "name": "edit_file",
    "description": (
        "Replace one occurrence of old_text with new_text in a file. old_text must match "
        "the file EXACTLY (including whitespace and indentation) and must appear EXACTLY "
        "ONCE — if it appears zero times or more than once, the edit is REJECTED and you "
        "must add surrounding lines to old_text to make it unique. Use this for partial "
        "edits; never rewrite a whole file with write_file. Returns 'Edited <path>' on "
        "success, or an 'Error: ...' string that explains how to fix the call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit."},
            "old_text": {
                "type": "string",
                "description": (
                    "Exact text to find, including indentation. Must match one unique "
                    "location; add surrounding lines if it would otherwise be ambiguous."
                ),
            },
            "new_text": {"type": "string", "description": "The replacement text."},
        },
        "required": ["path", "old_text", "new_text"],
    },
    # Format-sensitive tool → a schema-validated example of a well-formed, unique edit
    # (Anthropic: prefer input_examples for format-sensitive inputs).
    "input_examples": [
        {
            "path": "minicc/llm.py",
            "old_text": "TOKEN_BUDGET = 150_000",
            "new_text": "TOKEN_BUDGET = 120_000",
        }
    ],
}


def edit_file(path: str, old_text: str, new_text: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"Error: {path} does not exist"
    if not p.is_file():
        return f"Error: {path} is not a file"
    try:
        content = p.read_text()
    except Exception as e:
        return f"Error: {e}"

    count = content.count(old_text)
    if count == 0:
        return (
            f"Error: old_text not found in {path}. It must match exactly, including "
            f"whitespace and indentation."
        )
    if count > 1:
        return (
            f"Error: old_text appears {count} times in {path}; it must be unique. "
            f"Add surrounding lines to old_text so it matches exactly one location."
        )
    try:
        p.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"
