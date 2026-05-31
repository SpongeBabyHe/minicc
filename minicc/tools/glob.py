from pathlib import Path

MAX_MATCHES = 100  # CC's default

SCHEMA = {
    "name": "glob",
    "description": "Find files matching a glob pattern (e.g. '**/*.py', 'src/*.ts'). Returns up to 100 matching paths, sorted by modification time (most recent first). If more than 100 match, the result includes a truncation flag — narrow your pattern. Use this instead of 'bash find' or 'bash ls' for locating files by name pattern.",
    "input_schema": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
}


def glob(pattern: str) -> str:
    matches = list(Path('.').glob(pattern))
    if not matches:
        return "No matches."

    # Sort by mtime descending (CC behavior — most recent first)
    try:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        # Some paths might not be statable; fall back to lexical sort
        matches.sort(key=str)

    truncated = len(matches) > MAX_MATCHES
    shown = matches[:MAX_MATCHES]
    body = "\n".join(str(p) for p in shown)

    if truncated:
        body += (
            f"\n\n[Truncated: showing {MAX_MATCHES} most recent of {len(matches)} total "
            f"matches. Narrow your pattern to see more.]"
        )

    return body
