"""Source-aware user, shared-project, and project-local settings.

Settings are discovered as separate :class:`SettingsSource` objects before any
consumer-specific merge happens:

    ~/.minicc/settings.json                   (user-global)
    <cwd>/.minicc/settings.json               (shared project)
    <repo-root>/.minicc/settings.local.json   (project-local, machine-owned)

For compatibility with pre-canonicalization releases, a local settings file in
the launch directory is also read when it differs from the repository-wide
path. The repository-wide file has scalar precedence; permission arrays from
both remain represented.

Low-to-high precedence is user -> shared project -> project local. Scalar
settings use the highest source that defines them; array settings concatenate
and deduplicate in source order; hook groups concatenate without deduplication.

CLI startup discovers all sources as inert data, then binds a view containing
either user settings only, ordinary project configuration with grant fields
still gated, or the fully trusted snapshot. All consumers therefore share one
source selection instead of reading project files independently.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from minicc.workspace import local_settings_root, workspace_root


# Fallback when no settings file sets a default.
DEFAULT_MODEL = "claude-sonnet-4-6"

_MINICC_GITIGNORE = "*\n!.gitignore\n!settings.json\n"
_MISSING = object()
_TRUST_GATED_ARRAY_PATHS = {
    ("permissions", "allow"),
    ("permissions", "additionalDirectories"),
    ("allowed_tools",),
}


class SettingsError(ValueError):
    """A settings source exists but is not a valid JSON object."""


class SettingsScope(str, Enum):
    """A settings source whose value matches Claude Code's public scope name."""

    USER = "user"
    PROJECT_SHARED = "project"
    PROJECT_LOCAL = "local"


SettingsPath = str | tuple[str, ...]


@dataclass(frozen=True)
class SettingsSource:
    """One settings file plus the metadata lost by an eager dict merge.

    ``permission_anchor`` is the source-specific base for leading-slash
    permission rules: user settings use ``~/.minicc``, shared settings use the
    starting directory, and project-local settings keep that starting directory
    even when the file itself is stored at the workspace root.
    """

    scope: SettingsScope
    path: Path
    permission_anchor: Path
    values: dict[str, Any]


@dataclass(frozen=True)
class SettingsEntry:
    """One array entry paired with the settings source that declared it."""

    value: Any
    source: SettingsSource


@dataclass(frozen=True)
class SettingsSnapshot:
    """Inert settings sources discovered for one starting directory."""

    start_dir: Path
    sources: tuple[SettingsSource, ...]
    includes_project_sources: bool

    def scalar(self, path: SettingsPath, default: Any = None) -> Any:
        """Resolve a scalar with highest-source-wins precedence."""
        return _scalar(self.sources, path, default)

    def array(self, path: SettingsPath) -> list[Any]:
        """Concatenate and deduplicate an array across low-to-high sources."""
        return _array(self.sources, path)

    def entries(self, path: SettingsPath) -> list[SettingsEntry]:
        """Return every array entry without discarding source provenance."""
        return _entries(self.sources, path)

    def source(self, scope: SettingsScope) -> SettingsSource | None:
        """Return the highest-precedence loaded source for ``scope``, if any."""
        return next(
            (
                source
                for source in reversed(self.sources)
                if source.scope == scope
            ),
            None,
        )

    def view(
        self,
        *,
        trusted: bool,
        project_grants_trusted: bool | None = None,
        local_grants_trusted: bool | None = None,
    ) -> SettingsView:
        """Select generic project sources and separately gated grant fields."""
        if project_grants_trusted is None:
            project_grants_trusted = trusted
        if local_grants_trusted is None:
            local_grants_trusted = trusted
        return SettingsView(
            snapshot=self,
            trusted=trusted,
            project_grants_trusted=project_grants_trusted,
            local_grants_trusted=local_grants_trusted,
        )


@dataclass(frozen=True)
class SettingsView:
    """A snapshot filtered at the workspace Trust boundary.

    ``trusted`` enables ordinary project configuration after the generic
    workspace check. ``project_grants_trusted`` and ``local_grants_trusted``
    independently control capability-expanding fields from the shared and local
    project sources. Shared grants require exact Trust; local grants may also be
    admitted after Git establishes that the repository did not supply the file,
    or before generic Trust when the launch directory is the user's HOME.

    Authorization still inspects restrictive project rules in the inert
    snapshot because ``deny`` and ``ask`` do not expand capability.
    """

    snapshot: SettingsSnapshot
    trusted: bool
    project_grants_trusted: bool
    local_grants_trusted: bool

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
        if _path_parts(path) in _TRUST_GATED_ARRAY_PATHS:
            merged: list[Any] = []
            for entry in self.entries(path):
                if entry.value not in merged:
                    merged.append(entry.value)
            return merged
        return _array(self.sources, path)

    def entries(self, path: SettingsPath) -> list[SettingsEntry]:
        """Return source-preserving entries visible through this Trust view."""
        if _path_parts(path) not in _TRUST_GATED_ARRAY_PATHS:
            return _entries(self.sources, path)
        return [
            entry
            for entry in _entries(self.snapshot.sources, path)
            if entry.source.scope == SettingsScope.USER
            or (
                entry.source.scope == SettingsScope.PROJECT_SHARED
                and self.trusted
                and self.project_grants_trusted
            )
            or (
                entry.source.scope == SettingsScope.PROJECT_LOCAL
                and self.local_grants_trusted
            )
        ]


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


def _entries(
    sources: Iterable[SettingsSource], path: SettingsPath
) -> list[SettingsEntry]:
    entries: list[SettingsEntry] = []
    for source in sources:
        value = _lookup(source.values, path)
        if isinstance(value, list):
            entries.extend(SettingsEntry(entry, source) for entry in value)
    return entries


def _user_settings_path() -> Path:
    return Path.home() / ".minicc" / "settings.json"


def _shared_project_settings_path() -> Path:
    return Path.cwd() / ".minicc" / "settings.json"


def _local_project_settings_path() -> Path:
    return local_settings_root() / ".minicc" / "settings.local.json"


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


def discover_settings(
    start_dir: Path | None = None,
    *,
    include_project_sources: bool = True,
) -> SettingsSnapshot:
    """Read user settings and, when allowed, both project settings sources."""
    start = (start_dir or Path.cwd()).resolve()
    local_settings_dir = local_settings_root(start)
    user_path = _user_settings_path()
    paths = [(SettingsScope.USER, user_path, user_path.parent)]
    if include_project_sources:
        if start_dir is None:
            shared_project_path = _shared_project_settings_path()
            local_project_path = _local_project_settings_path()
        else:
            shared_project_path = start / ".minicc" / "settings.json"
            local_project_path = local_settings_dir / ".minicc" / "settings.local.json"
        paths.append(
            (
                SettingsScope.PROJECT_SHARED,
                shared_project_path,
                start,
            )
        )
        legacy_local_path = start / ".minicc" / "settings.local.json"
        paths.extend(
            (
                (SettingsScope.PROJECT_LOCAL, legacy_local_path, start),
                (SettingsScope.PROJECT_LOCAL, local_project_path, start),
            )
        )
    sources: list[SettingsSource] = []
    seen: set[Path] = set()
    for scope, path, permission_anchor in paths:
        canonical = path.resolve()
        if canonical in seen or not path.exists():
            continue
        seen.add(canonical)
        sources.append(
            SettingsSource(
                scope=scope,
                path=path,
                permission_anchor=permission_anchor,
                values=_read(path),
            )
        )
    return SettingsSnapshot(
        start_dir=start,
        sources=tuple(sources),
        includes_project_sources=include_project_sources,
    )


_ACTIVE_SETTINGS: SettingsView | None = None


def activate(view: SettingsView) -> None:
    """Bind the process-wide settings view selected by startup Trust."""
    global _ACTIVE_SETTINGS
    _ACTIVE_SETTINGS = view


def reset_active_settings() -> None:
    """Drop a bound view; the next getter sees only fresh user settings."""
    global _ACTIVE_SETTINGS
    _ACTIVE_SETTINGS = None


def refresh_active_settings() -> SettingsView:
    """Re-read the bound snapshot while preserving its Trust selection."""
    global _ACTIVE_SETTINGS
    if _ACTIVE_SETTINGS is None:
        return current_settings()
    previous = _ACTIVE_SETTINGS
    snapshot = discover_settings(
        previous.snapshot.start_dir,
        include_project_sources=previous.snapshot.includes_project_sources,
    )
    _ACTIVE_SETTINGS = snapshot.view(
        trusted=previous.trusted,
        project_grants_trusted=previous.project_grants_trusted,
        local_grants_trusted=previous.local_grants_trusted,
    )
    return _ACTIVE_SETTINGS


def current_settings() -> SettingsView:
    """Return the startup-bound view, defaulting to an untrusted user-only view."""
    if _ACTIVE_SETTINGS is not None:
        return _ACTIVE_SETTINGS
    return discover_settings(include_project_sources=False).view(trusted=False)


def config_roots(subdir: str):
    """Trusted project roots, bounded by the workspace, then the personal root."""
    view = current_settings()
    project_roots: list[Path] = []
    if view.trusted:
        start_dir = view.snapshot.start_dir
        boundary = workspace_root(start_dir)
        current = start_dir
        while True:
            project_roots.append(current / ".minicc" / subdir)
            if current == boundary:
                break
            current = current.parent
        yield from reversed(project_roots)
    personal_root = Path.home() / ".minicc" / subdir
    if personal_root not in project_roots:
        yield personal_root


def resolve_model() -> str:
    """Startup model: project-local > shared-project > user > default."""
    return current_settings().scalar("default_model", DEFAULT_MODEL) or DEFAULT_MODEL


def web_search_enabled() -> bool:
    """Whether to offer the server-side web_search tool (default true)."""
    return current_settings().scalar("web_search", True) is not False


def skill_shell_disabled() -> bool:
    """Resolve Claude Code's ``disableSkillShellExecution`` scalar setting."""
    return bool(current_settings().scalar("disableSkillShellExecution", False))


def resolve_cache_ttl() -> str:
    """Stable-prefix cache TTL: project-local > shared-project > user > ``5m``."""
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


def allowed_tools() -> list[str]:
    """Merged minicc-specific whole-tool startup grants."""
    return sorted(
        str(entry)
        for entry in current_settings().array("allowed_tools")
        if isinstance(entry, str)
    )


def _settings_path(scope: str) -> Path:
    if scope == "user":
        return _user_settings_path()
    if scope == "project":
        return _shared_project_settings_path()
    if scope == "local":
        return _local_project_settings_path()
    raise ValueError(f"unknown settings scope: {scope}")


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


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path != _user_settings_path():
        _ensure_minicc_gitignore(path.parent)
    path.write_text(json.dumps(data, indent=2) + "\n")
    if _ACTIVE_SETTINGS is not None:
        refresh_active_settings()


def set_default_model(model_id: str, scope: str = "user") -> Path:
    """Persist the model to user, shared-project, or project-local settings."""
    path = _settings_path(scope)
    data = _read(path)
    data["default_model"] = model_id
    _write(path, data)
    return path


def add_allow_rule(pattern: str) -> Path:
    """Persist one Bash allow rule to machine-local project settings."""
    path = _local_project_settings_path()
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
