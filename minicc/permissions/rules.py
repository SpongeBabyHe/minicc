"""Parse source-aware settings rules and match them against tool calls.

Rule loading retains each setting's provenance so leading-slash path patterns
use the correct permission anchor. This module owns matching only; precedence
and user interaction belong to :mod:`minicc.permissions.authorization`.
"""

import fnmatch
import os
import re
import urllib.parse
from functools import cache
from pathlib import Path, PurePosixPath

from minicc import config

from . import bash
from .models import PermissionEffect, PermissionRule


_RULE_SYNTAX = re.compile(r"^([A-Za-z_][A-Za-z0-9_*]*)(?:\((.*)\))?$", re.S)
_PARAMETER_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", re.S)
_TOOL_ALIASES = {
    "agent": "agent",
    "bash": "bash",
    "edit": "edit",
    "edit_file": "edit_file",
    "glob": "glob",
    "grep": "grep",
    "memory": "memory",
    "read": "read",
    "read_file": "read_file",
    "skill": "skill",
    "taskcreate": "task_create",
    "taskget": "task_get",
    "tasklist": "task_list",
    "taskupdate": "task_update",
    "webfetch": "web_fetch",
    "web_fetch": "web_fetch",
    "websearch": "web_search",
    "web_search": "web_search",
    "write": "write_file",
    "write_file": "write_file",
}
READ_TOOLS = frozenset({"read_file", "glob", "grep"})
EDIT_TOOLS = frozenset({"edit_file", "write_file"})
_READ_DENY_EDIT_TOOLS = frozenset({"edit_file"})
SERVER_TOOLS = frozenset({"web_search"})
_PATH_INPUTS = {
    "edit_file": "path",
    "grep": "path",
    "read_file": "path",
    "write_file": "path",
}
_PRIMARY_INPUTS = {
    "agent": "subagent_type",
    "glob": "pattern",
    "memory": "path",
    "skill": "skill",
}
_PRIMARY_CONTENT_FIELDS = {
    "agent": frozenset({"prompt"}),
    "bash": frozenset({"command"}),
    "edit_file": frozenset({"path"}),
    "glob": frozenset({"pattern"}),
    "grep": frozenset({"path"}),
    "read_file": frozenset({"path"}),
    "skill": frozenset({"skill"}),
    "web_fetch": frozenset({"url"}),
    "write_file": frozenset({"path"}),
}

_SETTINGS_RULES: list[PermissionRule] | None = None
_SETTINGS_RULES_VIEW: config.SettingsView | None = None


def normalize_tool_name(name: str) -> str | None:
    """Map settings and Skill aliases to minicc's canonical tool names."""
    normalized = name.casefold()
    if normalized == "*":
        return "*"
    if "*" in normalized:
        return normalized
    return _TOOL_ALIASES.get(normalized)


def _parse_rule(
    entry: config.SettingsEntry,
    effect: PermissionEffect,
) -> PermissionRule | None:
    raw = str(entry.value).strip()
    match = _RULE_SYNTAX.fullmatch(raw)
    if not match:
        return None
    tool_pattern = normalize_tool_name(match.group(1))
    if tool_pattern is None:
        return None
    if effect == PermissionEffect.ALLOW and "*" in tool_pattern:
        return None
    argument_pattern = match.group(2)
    compiled = (
        bash.compile_rule(argument_pattern)
        if tool_pattern == "bash" and argument_pattern is not None
        else None
    )
    return PermissionRule(
        effect=effect,
        raw=raw,
        tool_pattern=tool_pattern,
        argument_pattern=argument_pattern,
        source=entry.source,
        compiled=compiled,
    )


def parse_rules(
    entries: list[config.SettingsEntry],
    effect: PermissionEffect,
) -> list[PermissionRule]:
    """Parse valid settings entries without changing active permission state."""
    parsed: list[PermissionRule] = []
    for entry in entries:
        if rule := _parse_rule(entry, effect):
            parsed.append(rule)
    return parsed


def permission_rules() -> list[PermissionRule]:
    """Return effective rules while retaining source and permission anchor.

    Restrictive project rules remain active before Trust because they only
    remove authority. Project allow rules enter through the active settings
    view only after the workspace has been trusted.
    """
    global _SETTINGS_RULES, _SETTINGS_RULES_VIEW
    view = config.current_settings()
    if _SETTINGS_RULES is None or _SETTINGS_RULES_VIEW is not view:
        loaded: list[PermissionRule] = []
        for effect in (PermissionEffect.DENY, PermissionEffect.ASK):
            loaded.extend(
                parse_rules(
                    view.snapshot.entries(("permissions", effect.value)),
                    effect,
                )
            )
        loaded.extend(
            parse_rules(
                view.entries(("permissions", PermissionEffect.ALLOW.value)),
                PermissionEffect.ALLOW,
            )
        )
        _SETTINGS_RULES = loaded
        _SETTINGS_RULES_VIEW = view
    return _SETTINGS_RULES


def reset_cache() -> None:
    """Discard compiled settings rules so the next decision reloads them."""
    global _SETTINGS_RULES, _SETTINGS_RULES_VIEW
    _SETTINGS_RULES = None
    _SETTINGS_RULES_VIEW = None


def tool_matches(rule: PermissionRule, tool_name: str) -> bool:
    """Whether a rule's tool selector covers one canonical tool name."""
    if rule.tool_pattern == "read":
        covered = READ_TOOLS
        if rule.effect == PermissionEffect.DENY:
            covered = covered | _READ_DENY_EDIT_TOOLS
        return tool_name in covered
    if rule.tool_pattern == "edit":
        return tool_name in EDIT_TOOLS
    return fnmatch.fnmatchcase(tool_name, rule.tool_pattern)


def _path_pattern(rule: PermissionRule) -> str:
    pattern = rule.argument_pattern or ""
    if pattern.startswith("//"):
        return os.path.normpath("/" + pattern[2:])
    if pattern.startswith("~/"):
        return os.path.normpath(str(Path.home() / pattern[2:]))
    if pattern.startswith("/"):
        return os.path.normpath(str(rule.permission_anchor / pattern[1:]))
    if pattern.startswith("./"):
        pattern = pattern[2:]
    elif "/" not in pattern:
        pattern = f"**/{pattern}"
    elif (
        rule.effect != PermissionEffect.ALLOW
        and re.fullmatch(r"[^/]+/\*\*", pattern)
    ):
        pattern = f"**/{pattern}"
    working_dir = config.current_settings().snapshot.start_dir
    return os.path.normpath(str(working_dir / pattern))


def _path_glob_matches(path: str, pattern: str) -> bool:
    """Match a path glob where ``*`` is local and ``**`` is recursive."""
    path_parts = PurePosixPath(path).parts
    pattern_parts = PurePosixPath(pattern).parts

    @cache
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _domain_matches(host: str, pattern: str) -> bool:
    """Match WebFetch domains without allowing ``*`` to cross a dot."""
    host = host.casefold().rstrip(".")
    pattern = pattern.casefold().rstrip(".")
    if pattern == "*":
        return bool(host)
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return bool(suffix) and host.endswith("." + suffix)
    host_parts = host.split(".")
    pattern_parts = pattern.split(".")
    return len(host_parts) == len(pattern_parts) and all(
        fnmatch.fnmatchcase(host_part, pattern_part)
        for host_part, pattern_part in zip(host_parts, pattern_parts)
    )


def _url_host(url: object) -> str:
    try:
        return urllib.parse.urlparse(str(url)).hostname or ""
    except ValueError:
        return ""


def _parameter_rule_matches(
    rule: PermissionRule,
    tool_name: str,
    tool_input: dict,
) -> bool | None:
    """Match restrictive ``param:value`` syntax when it applies."""
    pattern = rule.argument_pattern
    if rule.effect == PermissionEffect.ALLOW or pattern is None:
        return None
    parameter_match = _PARAMETER_PATTERN.fullmatch(pattern)
    if parameter_match is None:
        return None
    field_name, expected = parameter_match.groups()
    if field_name in _PRIMARY_CONTENT_FIELDS.get(tool_name, ()):
        return None
    if tool_name == "bash" and field_name not in {"timeout"}:
        return None
    value = tool_input.get(field_name)
    if value is None or isinstance(value, (dict, list)):
        return False
    actual = str(value).lower() if isinstance(value, bool) else str(value)
    return fnmatch.fnmatchcase(actual, expected)


def _argument_matches(
    rule: PermissionRule,
    tool_name: str,
    tool_input: dict,
) -> bool:
    pattern = rule.argument_pattern
    if pattern is None:
        return True
    if tool_name == "web_fetch" and pattern.startswith("domain:"):
        return _domain_matches(_url_host(tool_input.get("url", "")), pattern[7:])
    if tool_name in _PATH_INPUTS:
        raw_path = str(tool_input.get(_PATH_INPUTS[tool_name], ""))
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = config.current_settings().snapshot.start_dir / target
        lexical_path = os.path.abspath(str(target))
        resolved_path = os.path.realpath(target)
        path_pattern = _path_pattern(rule)
        matches = {
            _path_glob_matches(path, path_pattern)
            for path in (lexical_path, resolved_path)
        }
        if rule.effect == PermissionEffect.ALLOW:
            return matches == {True}
        return True in matches
    parameter_result = _parameter_rule_matches(rule, tool_name, tool_input)
    if parameter_result is not None:
        return parameter_result
    field_name = _PRIMARY_INPUTS.get(tool_name)
    if field_name is None:
        return False
    value = str(tool_input.get(field_name, ""))
    if tool_name == "skill":
        args = str(tool_input.get("args", "")).strip()
        if args:
            value = f"{value} {args}"
    if tool_name in {"agent", "skill"}:
        return fnmatch.fnmatchcase(value.casefold(), pattern.casefold())
    return fnmatch.fnmatchcase(value, pattern)


def _rule_matches_call(
    rule: PermissionRule,
    tool_name: str,
    tool_input: dict,
) -> bool:
    if not tool_matches(rule, tool_name):
        return False
    if (
        rule.argument_pattern is not None
        and rule.tool_pattern in {"glob", "grep", "write_file"}
    ):
        parameter_result = _parameter_rule_matches(rule, tool_name, tool_input)
        if parameter_result is not None:
            return parameter_result
        return False
    if tool_name == "bash":
        parameter_result = _parameter_rule_matches(rule, tool_name, tool_input)
        if parameter_result is not None:
            return parameter_result
    if tool_name != "bash" or rule.argument_pattern is None:
        return _argument_matches(rule, tool_name, tool_input)
    return bash.rule_matches_command(
        rule,
        str(tool_input.get("command", "")),
    )


def matching_rule(
    effect: PermissionEffect,
    tool_name: str,
    tool_input: dict,
) -> PermissionRule | None:
    """Return the first effective rule of one outcome that matches a call."""
    return next(
        (
            rule
            for rule in permission_rules()
            if rule.effect == effect and _rule_matches_call(rule, tool_name, tool_input)
        ),
        None,
    )


def matches_all_uses(rule: PermissionRule) -> bool:
    """Whether a rule blocks every possible invocation of its selected tool."""
    if rule.argument_pattern is None:
        return True
    if rule.tool_pattern == "bash" and rule.argument_pattern.strip() == "*":
        return True
    return (
        rule.tool_pattern == "web_fetch"
        and rule.argument_pattern.casefold().strip() == "domain:*"
    )
