import subprocess

MAX_COUNT_PER_FILE = 100      # rg --max-count is per file, not total
MAX_OUTPUT_CHARS = 50_000     # final size cap on what we hand back

SCHEMA = {
    "name": "grep",
    "description": (
        "Search file contents for a regex pattern (ripgrep). Returns matching lines "
        "prefixed with `file:line:`, up to 100 matches PER FILE. Use it to find symbols, "
        "function definitions, or text references in the codebase — prefer it over "
        "`bash grep`/`find`. Output is capped at 50,000 chars (truncation is noted); an "
        "invalid regex returns an 'Error: ...' string."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The regex to search for."},
            "path": {
                "type": "string",
                "description": "File or directory to search. Defaults to the current directory.",
            },
        },
        "required": ["pattern"],
    },
}


def grep(pattern: str, path: str = ".") -> str:
    try:
        r = subprocess.run(
            [
                "rg",
                "--line-number",      # rg omits line numbers when output isn't a TTY;
                "--with-filename",    # force file:line: prefixes so the description holds
                f"--max-count={MAX_COUNT_PER_FILE}",
                pattern,
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return "Error: ripgrep (rg) not installed. Install with: brew install ripgrep"

    # rg exit codes: 0 = match, 1 = no match, 2 = error (e.g. bad regex). Surface the
    # error instead of silently returning "No matches." (which looks like a clean miss).
    if r.returncode >= 2:
        return f"Error: {(r.stderr or 'ripgrep failed').strip()}"
    if not r.stdout:
        return "No matches."

    out = r.stdout
    if len(out) > MAX_OUTPUT_CHARS:
        out = (
            out[:MAX_OUTPUT_CHARS]
            + f"\n\n[Truncated at {MAX_OUTPUT_CHARS} chars — narrow your pattern or path.]"
        )
    return out
