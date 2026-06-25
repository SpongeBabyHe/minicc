"""User/project settings: model preference + a tool allowlist.

Two optional JSON files:

    ~/.minicc/settings.json      (global)
    <cwd>/.minicc/settings.json  (project)

Keys:
  default_model  — project overrides global, then DEFAULT_MODEL. A session
                   `/model X` overrides on top (in-process, not persisted).
  allowed_tools  — gated tools pre-approved without a prompt; union of global +
                   project. bash is NOT preloadable (unbounded + irreversible —
                   approve per session). Keep trust project-scoped. See PERMISSIONS.md.

env is reserved for secrets + endpoint (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL).
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


def _tool_list(d: dict) -> set:
    v = d.get("allowed_tools", [])
    return set(v) if isinstance(v, list) else set()


def allowed_tools() -> list:
    """Tools listed for pre-approval in settings (union of global + project).
    permissions.preload decides what's actually applied (bash is excluded there).
    minicc never auto-writes here — you opt in by editing settings.json. Keep
    trust project-scoped. See PERMISSIONS.md."""
    return sorted(_tool_list(_read(_global())) | _tool_list(_read(_project())))
