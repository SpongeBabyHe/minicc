import subprocess

SCHEMA = {
    "name": "grep",
    "description": "Search for a regex pattern across files in a directory. Use this to find symbols, function definitions, or text references in the codebase. Returns matching lines with file:line prefixes, capped at 100 matches.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["pattern"],
    }
}


def grep(pattern: str, path: str = ".") -> str:
    try:
        r = subprocess.run(
            [
                "rg",
                "--max-count=100",
                pattern, path
            ],
            capture_output=True,
            text=True,
            timeout=30)
        return (r.stdout or "No matches.")[:50000]
    except FileNotFoundError:
        return "Error: ripgrep (rg) not installed. Install with: brew install ripgrep"
