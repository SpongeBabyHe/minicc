"""User/project settings: the model preference.

Two optional JSON files, project overrides global:

    ~/.minicc/settings.json      (global — your default everywhere)
    <cwd>/.minicc/settings.json  (project — overrides global)

Precedence: project default_model > global default_model > DEFAULT_MODEL; a session `/model X` overrides on top (in-process, not persisted).
"""

import json
from pathlib import Path

# Fallback when no settings file sets a default.
DEFAULT_MODEL = "claude-sonnet-4-6"


def _global() -> Path:
    return Path.home() / ".minicc" / "settings.json"


def _project() -> Path:
    return Path.cwd() / ".minicc" / "settings.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def resolve_model() -> str:
    """Startup model: project default_model > global default_model > DEFAULT_MODEL."""
    g, p = _read(_global()), _read(_project())
    return p.get("default_model") or g.get("default_model") or DEFAULT_MODEL


def set_default_model(model_id: str, scope: str = "global") -> Path:
    """Persist default_model to the global (default) or project settings file."""
    path = _global() if scope == "global" else _project()
    path.parent.mkdir(parents=True, exist_ok=True)
    if scope == "project":
        # keep project .minicc/ self-ignoring, like sessions + repl_history
        gi = path.parent / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")
    data = _read(path)
    data["default_model"] = model_id
    path.write_text(json.dumps(data, indent=2))
    return path
