from pathlib import Path

from minicc.tools import freshness

SCHEMA = {
    "name": "edit_file",
    "description": (
        "Replace one occurrence of old_text with new_text in a file. old_text must match "
        "the file EXACTLY (including whitespace and indentation) and must appear EXACTLY "
        "ONCE — if it appears zero times or more than once, the edit is REJECTED and you "
        "must add surrounding lines to old_text to make it unique. Use this for partial "
        "edits; never rewrite a whole file with write_file. The file must have been "
        "read with read_file first, and is rejected if it changed on disk since that "
        "read — re-read and retry. On success returns the edited region with line "
        "numbers (no need to read the file back); on failure an 'Error: ...' string "
        "that explains how to fix the call."
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
    stale = freshness.check(p)  # CC's read-before-edit + staleness contract
    if stale:
        return stale
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
        updated = content.replace(old_text, new_text, 1)
        p.write_text(updated)
    except Exception as e:
        return f"Error: {e}"
    freshness.record(p)  # our own edit stays "fresh" — consecutive edits need no re-read
    return f"Edited {path} {_snippet(updated, content.index(old_text), new_text)}"


def _snippet(updated: str, at: int, new_text: str) -> str:
    """The edited region with line numbers and ±2 context lines — post-edit
    visibility without a read-back call (minicc's lightweight version of CC's
    file-state-current-in-context mechanism). Long insertions are elided."""
    start_line = updated[:at].count("\n")               # 0-based first edited line
    n_lines = new_text.count("\n") + 1
    lines = updated.splitlines()
    lo, hi = max(0, start_line - 2), min(len(lines), start_line + n_lines + 2)
    window = list(range(lo, hi))
    if len(window) > 14:                                 # elide huge insertions
        window = window[:7] + [None] + window[-6:]
    body = "\n".join(
        "     ..." if i is None else f"{i + 1:6}\t{lines[i]}" for i in window
    )
    return f"(lines {start_line + 1}-{start_line + n_lines}):\n{body}"
