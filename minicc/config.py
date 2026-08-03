"""Source-aware user, project, and local settings.

Settings are discovered as separate :class:`SettingsSource` objects before any
consumer-specific merge happens:

    ~/.minicc/settings.json                   (user)
    <cwd>/.minicc/settings.json               (shared project)
    <repo-root>/.minicc/settings.local.json   (local, machine-owned)

Low-to-high precedence is user -> project -> local. Scalar settings use the
highest source that defines them; array settings concatenate and deduplicate in
source order; hook groups concatenate without deduplication.

The active-view seam exists for workspace Trust: today, before TrustManager is
wired into startup, an unbound process reads a fresh full view on each call.
Later startup code can bind a restricted or trusted view once and all existing
consumers will observe the same source selection without re-reading project
files themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


# Fallback when no settings file sets a default.
DEFAULT_MODEL = "claude-sonnet-4-6"

_MINICC_GITIGNORE = "*\n!.gitignore\n!settings.json\n"
_MISSING = object()


class SettingsError(ValueError):
    """A settings source exists but is not a valid JSON object."""


class SettingsScope(str, Enum):
    """A concrete minicc settings source, ordered separately by the snapshot."""

    USER = "user"
    PROJECT = "project"
    LOCAL = "local"


SettingsPath = str | tuple[str, ...]


@dataclass(frozen=True)
class SettingsSource:
    """One settings file plus the metadata lost by an eager dict merge.

    ``anchor`` is retained for the future permission engine: source-relative
    paths do not necessarily resolve from the settings file's parent.
    """

    scope: SettingsScope
    path: Path
    anchor: Path
    values: dict[str, Any]


def _path_parts(path: SettingsPath) -> tuple[str, ...]:
    return (path,) if isinstance(path, str) else path


def _lookup(values: dict[str, Any], path: SettingsPath) -> Any:
    current: Any = values
    for part in _path_parts(path):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _scalar(
    sources: Iterable[SettingsSource], path: SettingsPath, default: Any = None
) -> Any:
    for source in reversed(tuple(sources)):
        value = _lookup(source.values, path)
        if value is not _MISSING:
            return value
    return default


def _array(sources: Iterable[SettingsSource], path: SettingsPath) -> list[Any]:
    merged: list[Any] = []
    for source in sources:
        value = _lookup(source.values, path)
        if not isinstance(value, list):
            continue
        for entry in value:
            if entry not in merged:
                merged.append(entry)
    return merged


@dataclass(frozen=True)
class SettingsSnapshot:
    """Inert settings sources discovered for one starting directory."""

    start_dir: Path
    sources: tuple[SettingsSource, ...]

    def scalar(self, path: SettingsPath, default: Any = None) -> Any:
        """Resolve a scalar with highest-source-wins precedence."""
        return _scalar(self.sources, path, default)

    def array(self, path: SettingsPath) -> list[Any]:
        """Concatenate and deduplicate an array across low-to-high sources."""
        return _array(self.sources, path)

    def source(self, scope: SettingsScope) -> SettingsSource | None:
        """Return the loaded source for ``scope``, if that file exists."""
        return next((source for source in self.sources if source.scope == scope), None)

    def view(self, *, trusted: bool) -> SettingsView:
        """Select the sources visible before or after workspace Trust."""
        return SettingsView(snapshot=self, trusted=trusted)


@dataclass(frozen=True)
class SettingsView:
    """A snapshot filtered at the workspace Trust boundary.

    User settings are always visible. Project and local settings enter the
    effective runtime only after Trust. The current CLI binds no view yet, so
    :func:`current_settings` supplies a fresh trusted view for compatibility.
    """

    snapshot: SettingsSnapshot
    trusted: bool

    @property
    def sources(self) -> tuple[SettingsSource, ...]:
        if self.trusted:
            return self.snapshot.sources
        return tuple(
            source
            for source in self.snapshot.sources
            if source.scope == SettingsScope.USER
        )

    def scalar(self, path: SettingsPath, default: Any = None) -> Any:
        """Resolve a scalar from the sources selected by this view."""
        return _scalar(self.sources, path, default)

    def array(self, path: SettingsPath) -> list[Any]:
        """Merge an array from the sources selected by this view."""
        return _array(self.sources, path)

_ACTIVE_SETTINGS: SettingsView | None = None


def _global() -> Path:
    return Path.home() / ".minicc" / "settings.json"


def _project() -> Path:
    return Path.cwd() / ".minicc" / "settings.json"


def _repository_root(start: Path | None = None) -> Path | None:
    """Nearest ancestor containing a filesystem ``.git`` entry.

    The entry may be a directory or a worktree/submodule file. Its contents are
    deliberately not followed; repository-controlled ``commondir`` data must
    never redirect a later workspace Trust identity.
    """
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        marker = directory / ".git"
        if marker.exists() or marker.is_symlink():
            return directory
    return None


def _local() -> Path:
    root = _repository_root() or Path.cwd()
    return root / ".minicc" / "settings.local.json"


def config_roots(subdir: str):
    """The `.minicc/<subdir>/` directories to scan, in override order: project
    (cwd + every ancestor, outermost first) then personal (`~/.minicc`), so a
    later root wins a name clash. Shared by skills and agents discovery."""
    cwd = Path.cwd()
    for directory in [*reversed(cwd.parents), cwd]:
        yield directory / ".minicc" / subdir
    yield Path.home() / ".minicc" / subdir


def _ensure_minicc_gitignore(minicc_dir: Path) -> None:
    """Ignore runtime/local state while keeping shared settings trackable.

    The exact legacy file written by older minicc versions is migrated. A
    user-customized `.gitignore` is left untouched.
    """
    gitignore = minicc_dir / ".gitignore"
    if not gitignore.exists() or gitignore.read_text() == "*\n":
        gitignore.write_text(_MINICC_GITIGNORE)


def ensure_project_dir(subdir: str = "") -> Path:
    """Create `.minicc/<subdir>` under cwd and maintain its ignore policy."""
    root = Path.cwd() / ".minicc"
    directory = root / subdir if subdir else root
    directory.mkdir(parents=True, exist_ok=True)
    _ensure_minicc_gitignore(root)
    return directory


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise SettingsError(f"invalid JSON in {path}: {error.msg}") from error
    if not isinstance(data, dict):
        raise SettingsError(f"settings source must contain a JSON object: {path}")
    return data


def discover_settings(start_dir: Path | None = None) -> SettingsSnapshot:
    """Read existing sources as inert JSON, preserving low-to-high precedence."""
    if start_dir is None:
        start = Path.cwd().resolve()
        project_path = _project()
        local_path = _local()
    else:
        start = start_dir.resolve()
        project_path = start / ".minicc" / "settings.json"
        local_root = _repository_root(start) or start
        local_path = local_root / ".minicc" / "settings.local.json"
    paths = (
        (SettingsScope.USER, _global(), _global().parent),
        (SettingsScope.PROJECT, project_path, start),
        (SettingsScope.LOCAL, local_path, start),
    )
    sources: list[SettingsSource] = []
    seen: set[Path] = set()
    for scope, path, anchor in paths:
        canonical = path.resolve()
        if canonical in seen or not path.exists():
            continue
        seen.add(canonical)
        sources.append(
            SettingsSource(
                scope=scope,
                path=path,
                anchor=anchor,
                values=_read(path),
            )
        )
    return SettingsSnapshot(start_dir=start, sources=tuple(sources))


def activate(view: SettingsView) -> None:
    """Bind the process-wide settings view selected by startup Trust."""
    global _ACTIVE_SETTINGS
    _ACTIVE_SETTINGS = view


def reset_active_settings() -> None:
    """Drop a bound view; the next getter discovers a fresh full snapshot."""
    global _ACTIVE_SETTINGS
    _ACTIVE_SETTINGS = None


def current_settings() -> SettingsView:
    """Return the bound view, or a fresh full view until Trust startup lands."""
    if _ACTIVE_SETTINGS is not None:
        return _ACTIVE_SETTINGS
    return discover_settings().view(trusted=True)


def resolve_model() -> str:
    """Startup model: local > project > user > :data:`DEFAULT_MODEL`."""
    return current_settings().scalar("default_model", DEFAULT_MODEL) or DEFAULT_MODEL


def _settings_path(scope: str) -> Path:
    if scope in ("global", "user"):
        return _global()
    if scope == "project":
        return _project()
    if scope == "local":
        return _local()
    raise ValueError(f"unknown settings scope: {scope}")


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path != _global():
        _ensure_minicc_gitignore(path.parent)
    path.write_text(json.dumps(data, indent=2) + "\n")


def set_default_model(model_id: str, scope: str = "global") -> Path:
    """Persist ``default_model`` to user, project, or local settings."""
    path = _settings_path(scope)
    data = _read(path)
    data["default_model"] = model_id
    _write(path, data)
    return path


def web_search_enabled() -> bool:
    """Whether to offer the server-side web_search tool (default true)."""
    return current_settings().scalar("web_search", True) is not False


def skill_shell_disabled() -> bool:
    """Resolve Claude Code's ``disableSkillShellExecution`` scalar setting."""
    return bool(current_settings().scalar("disableSkillShellExecution", False))


def resolve_cache_ttl() -> str:
    """Stable-prefix cache TTL: local > project > user > ``5m``."""
    ttl = current_settings().scalar("cache_ttl", "5m") or "5m"
    return ttl if ttl in ("5m", "1h") else "5m"


def load_hooks() -> tuple[dict, bool]:
    """Return source-concatenated hook events plus scalar disable state."""
    view = current_settings()
    merged: dict[str, list[Any]] = {}
    for source in view.sources:
        hooks = source.values.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, groups in hooks.items():
            if isinstance(groups, list):
                merged.setdefault(event, []).extend(groups)
    return merged, bool(view.scalar("disableAllHooks", False))


_BASH_RULE = re.compile(r"^[Bb]ash\((.+)\)$")


def permission_allow_rules() -> list[str]:
    """Bash patterns from merged ``permissions.allow`` settings."""
    rules: list[str] = []
    for entry in current_settings().array(("permissions", "allow")):
        match = _BASH_RULE.match(str(entry).strip())
        if match and match.group(1) not in rules:
            rules.append(match.group(1))
    return rules


def add_allow_rule(pattern: str) -> Path:
    """Persist one Bash allow rule to machine-local project settings."""
    path = _local()
    data = _read(path)
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        data["permissions"] = permissions
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []
        permissions["allow"] = allow
    rule = f"bash({pattern})"
    if rule not in allow:
        allow.append(rule)
        _write(path, data)
    return path


def allowed_tools() -> list[str]:
    """Merged minicc-specific whole-tool startup grants."""
    return sorted(
        str(entry)
        for entry in current_settings().array("allowed_tools")
        if isinstance(entry, str)
    )
