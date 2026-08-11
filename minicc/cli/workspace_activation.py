"""Interactive Workspace Trust preview and source-aware settings activation.

This module owns the startup decision that turns an inert settings snapshot into
the process-wide view. Trust persistence remains in :mod:`minicc.trust`, path
identity and Git provenance remain in :mod:`minicc.workspace`, and the CLI only
controls when activation happens.
"""

import re
from pathlib import Path

from rich.text import Text

from minicc import config, permissions, trust, ux
from minicc.workspace import local_settings_are_repository_supplied


_TRUST_PREVIEW_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TRUST_PREVIEW_VALUE_LIMIT = 60
_TRUST_PREVIEW_HIGH_IMPACT_TOOLS = {
    "bash",
    "powershell",
    "write",
    "edit",
    "multiedit",
    "notebookedit",
    "webfetch",
    "websearch",
    "write_file",
    "edit_file",
    "web_fetch",
    "web_search",
}


def _workspace_trust_entries(
    snapshot: config.SettingsSnapshot,
    *paths: config.SettingsPath,
    include_local: bool = True,
) -> list[config.SettingsEntry]:
    return [
        entry
        for path in paths
        for entry in snapshot.entries(path)
        if entry.source.scope == config.SettingsScope.PROJECT_SHARED
        or (
            include_local
            and entry.source.scope == config.SettingsScope.PROJECT_LOCAL
        )
    ]


def _workspace_trust_permission_entries(
    snapshot: config.SettingsSnapshot,
    *,
    include_local: bool = True,
) -> list[config.SettingsEntry]:
    parsed = permissions.parse_rules(
        _workspace_trust_entries(
            snapshot,
            ("permissions", "allow"),
            include_local=include_local,
        ),
        permissions.PermissionEffect.ALLOW,
    )
    entries = [config.SettingsEntry(rule.raw, rule.source) for rule in parsed]
    for entry in _workspace_trust_entries(
        snapshot,
        "allowed_tools",
        include_local=include_local,
    ):
        if (
            isinstance(entry.value, str)
            and entry.value in permissions.GATED_TOOLS
            and entry.value not in permissions.NO_PRELOAD
        ):
            entries.append(entry)
    return entries


def _workspace_trust_directory_entries(
    snapshot: config.SettingsSnapshot,
    *,
    include_local: bool = True,
) -> list[config.SettingsEntry]:
    return [
        entry
        for entry in _workspace_trust_entries(
            snapshot,
            ("permissions", "additionalDirectories"),
            include_local=include_local,
        )
        if isinstance(entry.value, str)
    ]


def _trust_preview_value(value: object) -> str:
    text = Text.from_ansi(str(value)).plain
    text = _TRUST_PREVIEW_CONTROL_CHARS.sub("", text).strip()
    if len(text) > _TRUST_PREVIEW_VALUE_LIMIT:
        return text[:_TRUST_PREVIEW_VALUE_LIMIT] + "…"
    return text


def _trust_preview_values(entries: list[config.SettingsEntry]) -> list[str]:
    values: list[str] = []
    for entry in entries:
        value = _trust_preview_value(entry.value)
        if value and value not in values:
            values.append(value)
    return values


def _trust_preview_sources(entries: list[config.SettingsEntry]) -> list[str]:
    labels = {
        config.SettingsScope.PROJECT_SHARED: ".minicc/settings.json",
        config.SettingsScope.PROJECT_LOCAL: ".minicc/settings.local.json",
    }
    sources: list[str] = []
    for entry in entries:
        label = labels.get(entry.source.scope, str(entry.source.path))
        if label not in sources:
            sources.append(label)
    return sources


def _permission_preview_priority(rule: str) -> int:
    tool_name, separator, _content = rule.partition("(")
    tool_name = tool_name.strip().casefold()
    high_impact = (
        tool_name in _TRUST_PREVIEW_HIGH_IMPACT_TOOLS
        or tool_name.startswith("mcp__")
    )
    if high_impact:
        return 1 if separator else 0
    return 3 if separator else 2


def _directory_preview_priority(directory: str) -> int:
    if Path(directory).is_absolute() or directory.startswith("~"):
        return 0
    if ".." in directory:
        return 1
    return 2


def _format_trust_preview(values: list[str], limit: int) -> str:
    if len(values) > limit:
        shown = values[:limit]
        return f"{', '.join(shown)}, and {len(values) - limit} more"
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _confirm_workspace_trust(
    workspace_dir: Path,
    trust_identity: Path,
    snapshot: config.SettingsSnapshot,
    *,
    covered_by_parent: bool = False,
    local_project_grants_enabled: bool = False,
) -> bool:
    """Ask for exact Trust of ``workspace_dir`` and show gated fields."""
    permission_entries = _workspace_trust_permission_entries(
        snapshot,
        include_local=not local_project_grants_enabled,
    )
    directory_entries = _workspace_trust_directory_entries(
        snapshot,
        include_local=not local_project_grants_enabled,
    )
    ux.console.rule()
    ux.say("Accessing workspace:", style=ux.S_INFO)
    ux.say(repr(str(workspace_dir)))
    if trust_identity != workspace_dir:
        ux.say("Trust applies to repository:", style=ux.S_INFO)
        ux.say(repr(str(trust_identity)))
    ux.say(
        "Only continue for a project you created or trust. minicc can read, "
        "edit, and execute files here; project settings, hooks, skills, agents, "
        ".env values, and CLAUDE.md instructions will become active.",
        style=ux.S_INFO,
    )
    if permission_entries:
        count = len(permission_entries)
        sources = _format_trust_preview(
            _trust_preview_sources(permission_entries),
            limit=2,
        )
        rules = sorted(
            _trust_preview_values(permission_entries),
            key=_permission_preview_priority,
        )
        ux.say(
            f"This folder pre-approves {count} tool "
            f"{'permission' if count == 1 else 'permissions'} in {sources}:",
            style=ux.S_INFO,
        )
        ux.say(
            "  "
            + (
                _format_trust_preview(rules, limit=8)
                if rules
                else "(rule names contain unprintable characters)"
            ),
            style=ux.S_INFO,
        )
    if directory_entries:
        count = len(directory_entries)
        sources = _format_trust_preview(
            _trust_preview_sources(directory_entries),
            limit=2,
        )
        directories = sorted(
            _trust_preview_values(directory_entries),
            key=_directory_preview_priority,
        )
        ux.say(
            f"This folder adds {count} "
            f"{'directory' if count == 1 else 'directories'} to the workspace "
            f"in {sources}:",
            style=ux.S_INFO,
        )
        ux.say(
            "  "
            + (
                _format_trust_preview(directories, limit=6)
                if directories
                else "(directory names contain unprintable characters)"
            ),
            style=ux.S_INFO,
        )
    if permission_entries:
        ux.say(
            "These tool permissions will apply without asking. Only proceed "
            "if you trust this configuration.",
            style=ux.S_INFO,
        )
    if directory_entries:
        ux.say(
            "minicc shows additionalDirectories for Trust review, but does not "
            "yet enforce Claude Code's filesystem boundary.",
            style=ux.S_INFO,
        )
    prompt = (
        "Trust this folder? [yes/no — no continues without these permissions]: "
        if covered_by_parent
        else "Trust this folder? [yes/no]: "
    )
    answer = input(prompt).strip().lower()
    return answer in ("1", "y", "yes")


def activate_workspace_settings() -> bool:
    """Bind the Trust-filtered settings view for the launch directory.

    A fresh decline continues user-only with project ``deny``/``ask`` rules.
    Parent-covered workspaces activate ordinary project configuration, while an
    exact decision or local-file provenance independently controls grant fields.
    """
    launch_dir = Path.cwd().resolve()
    snapshot = config.discover_settings(launch_dir)
    local_project_grants_enabled = launch_dir == Path.home().resolve()
    config.activate(
        snapshot.view(
            project_configuration_enabled=False,
            local_project_grants_enabled=local_project_grants_enabled,
        )
    )
    manager = trust.TrustManager()
    identity = manager.workspace_identity(launch_dir)
    if manager.is_explicitly_trusted(identity):
        config.activate(snapshot.view(project_configuration_enabled=True))
        return True
    covered_by_parent = manager.is_trusted(identity)
    if covered_by_parent:
        local_project_grants_enabled = not local_settings_are_repository_supplied(
            launch_dir
        )
        config.activate(
            snapshot.view(
                project_configuration_enabled=True,
                shared_project_grants_enabled=False,
                local_project_grants_enabled=local_project_grants_enabled,
            )
        )
        if not (
            _workspace_trust_permission_entries(
                snapshot,
                include_local=not local_project_grants_enabled,
            )
            or _workspace_trust_directory_entries(
                snapshot,
                include_local=not local_project_grants_enabled,
            )
        ):
            return True
    if not _confirm_workspace_trust(
        launch_dir,
        identity,
        snapshot,
        covered_by_parent=covered_by_parent,
        local_project_grants_enabled=local_project_grants_enabled,
    ):
        if covered_by_parent:
            ux.say(
                "continuing without this workspace's project allow rules or "
                "additional directories",
                style=ux.S_INFO,
            )
            return True
        ux.say(
            "continuing in restricted mode; project deny/ask rules remain "
            "active, while project permissions and customizations stay disabled",
            style=ux.S_INFO,
        )
        return False
    manager.accept(identity)
    config.activate(snapshot.view(project_configuration_enabled=True))
    return True
