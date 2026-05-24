from pathlib import Path

SCHEMA = {
    "name": "glob",
    "description": "Find files matching a glob pattern (e.g. '**/*.py', 'src/*.ts'). Returns a newline-separated list of paths relative to the current directory, sorted. Use this instead of 'bash find' or 'bash ls' for locating files by name pattern.",
    "input_schema": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
}


def glob(pattern: str) -> str:
    matches = sorted(str(p) for p in Path.cwd().glob(pattern))
    return "\n".join(matches) if matches else "No matches."
