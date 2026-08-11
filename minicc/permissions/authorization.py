"""Stateful authorization orchestration for local and server-side tools.

Settings rules are resolved in ``deny -> ask -> allow`` order before built-in,
session, Skill, or Hook grants. This module owns ephemeral grants and approval
UI; it decides whether a call may run but does not sandbox an approved handler.
"""

import re
import sys
from pathlib import Path

from minicc import config, ux

from . import bash, rules
from .models import AuthorizationResult, PermissionEffect, PermissionRule


# Gated tools require approval unless policy or a scoped grant allows them.
GATED_TOOLS = ["bash", "write_file", "edit_file", "memory", "web_fetch"]

# Multi-command tools gate only the named mutating commands.
_GATED_COMMANDS = {"memory": {"create", "str_replace", "delete"}}

# Persistent Bash grants must use a narrow ``permissions.allow`` rule.
NO_PRELOAD = {"bash"}

# Session-wide grants created by the interactive ``all`` answer.
_ALLOWED: set[str] = set()

# Skill frontmatter grants live only until the next user prompt.
_SKILL_RULES: list[tuple[str, re.Pattern]] = []
_SKILL_TOOLS: set[str] = set()
_SKILL_BASH_RULE = re.compile(r"(?is)^bash\((.+)\)$")


def derive_rules(command: str) -> list[str]:
    """Derive bounded Bash patterns offered by the ``always`` choice."""
    return bash.derive_rules(command, rules.permission_rules(), _SKILL_RULES)


def _requires_prompt_by_default(tool_name: str, tool_input: dict) -> bool:
    if tool_name not in GATED_TOOLS:
        return False
    gated_commands = _GATED_COMMANDS.get(tool_name)
    if gated_commands is not None:
        return tool_input.get("command") in gated_commands
    return True


def _auto_allowed(
    tool_name: str,
    tool_input: dict,
    hook_allow: bool = False,
) -> bool:
    if hook_allow or tool_name in _ALLOWED or tool_name in _SKILL_TOOLS:
        return True
    if tool_name == "bash":
        return bash.command_is_allowed(
            str(tool_input.get("command", "")),
            rules.permission_rules(),
            _SKILL_RULES,
        )
    if rules.matching_rule(PermissionEffect.ALLOW, tool_name, tool_input):
        return True
    return not _requires_prompt_by_default(tool_name, tool_input)


def _is_gated(tool_name: str, tool_input: dict) -> bool:
    """Whether a call is not currently auto-authorized.

    Policy denial also returns true: compatibility callers use this helper to
    ask whether execution may proceed silently, not whether a prompt will
    necessarily be shown.
    """
    if rules.matching_rule(PermissionEffect.DENY, tool_name, tool_input):
        return True
    if rules.matching_rule(PermissionEffect.ASK, tool_name, tool_input):
        return True
    return not _auto_allowed(tool_name, tool_input)


def filter_tools(tools: list[dict]) -> list[dict]:
    """Remove tools disabled for every call before advertising them.

    ``web_search`` runs inside the Messages API, so minicc cannot pause at an
    individual invocation. Any deny or ask rule for that server tool therefore
    fails closed by hiding it rather than bypassing scoped policy.
    """
    permission_rules = rules.permission_rules()
    blocking = [rule for rule in permission_rules if rules.matches_all_uses(rule)]
    filtered = [
        tool
        for tool in tools
        if not any(
            rules.tool_matches(rule, tool["name"])
            and (
                rule.effect == PermissionEffect.DENY
                or (
                    rule.effect == PermissionEffect.ASK
                    and tool["name"] in rules.SERVER_TOOLS
                )
            )
            for rule in blocking
        )
        and not (
            tool["name"] in rules.SERVER_TOOLS
            and any(
                rule.effect in (PermissionEffect.DENY, PermissionEffect.ASK)
                and rules.tool_matches(rule, tool["name"])
                for rule in permission_rules
            )
        )
    ]
    return tools if len(filtered) == len(tools) else filtered


def _format_args(tool_name: str, tool_input: dict) -> str:
    if tool_name == "bash":
        return ux.kv_block(
            [
                ("cwd", Path.cwd()),
                ("cmd", tool_input.get("command", "")),
            ]
        )
    if tool_name == "write_file":
        content = tool_input.get("content", "")
        return ux.kv_block(
            [
                ("path", tool_input.get("path", "")),
                ("size", f"{len(content)} bytes"),
                ("preview", ux.truncate(content, 500)),
            ]
        )
    if tool_name == "edit_file":
        return ux.diff_view(
            tool_input.get("old_text", ""),
            tool_input.get("new_text", ""),
            tool_input.get("path", ""),
        )
    if tool_name == "memory":
        body = tool_input.get("file_text") or tool_input.get("new_str") or ""
        return ux.kv_block(
            [
                ("memory", tool_input.get("command", "")),
                ("path", tool_input.get("path", "")),
                ("preview", ux.truncate(body, 500)),
            ]
        )
    if tool_name == "web_fetch":
        return ux.kv_block(
            [
                ("url", tool_input.get("url", "")),
                ("extract", ux.truncate(tool_input.get("prompt", ""), 200)),
            ]
        )
    return ux.kv_block(list(tool_input.items()))


def _read_answer(prompt: str) -> str:
    """Read a non-empty approval answer after flushing stale TTY input."""
    try:
        import termios

        if sys.stdin.isatty():
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    while True:
        answer = input(prompt).strip().lower()
        if answer:
            return answer
        ux.say(
            "(empty answer ignored — type yes, no, all, or always)",
            style=ux.S_INFO,
        )


def _prompt(
    tool_name: str,
    tool_input: dict,
    *,
    one_time_only: bool,
    requested_by: str | None = None,
) -> bool:
    ux.say(_format_args(tool_name, tool_input))
    save_rules = (
        derive_rules(str(tool_input.get("command", "")))
        if (
            tool_name == "bash"
            and not one_time_only
            and config.current_settings().local_project_grants_enabled
        )
        else []
    )
    options = "[yes/no]" if one_time_only else "[yes/no/all]"
    if save_rules and not one_time_only:
        ux.say(
            f"always = don't ask again for: {', '.join(save_rules)}  "
            "(saved to local settings)",
            style=ux.S_INFO,
        )
        options = "[yes/no/all/always]"
    source = f" ({requested_by})" if requested_by else ""
    answer = _read_answer(f"Approve{source}? {options}: ")
    if answer == "all" and not one_time_only:
        _ALLOWED.add(tool_name)
        return True
    if answer == "always" and save_rules:
        for rule in save_rules:
            config.add_allow_rule(rule)
        return True
    return answer == "yes"


def _rule_reason(rule: PermissionRule) -> str:
    return (
        f"Denied by {rule.source.scope.value} permission rule {rule.raw!r} "
        f"from {str(rule.source.path)!r}."
    )


def _ask_source(rule: PermissionRule) -> str:
    return (
        f"{rule.source.scope.value} permission rule {rule.raw!r} "
        f"from {str(rule.source.path)!r}"
    )


def authorize(
    tool_name: str,
    tool_input: dict,
    *,
    hook_allow: bool = False,
    hook_ask: bool = False,
) -> AuthorizationResult:
    """Resolve one call with ``deny -> ask -> allow`` precedence.

    Hook and Skill grants can pre-approve a call, but neither can bypass a
    matching settings deny or ask rule. A forced prompt is one-shot because a
    broader choice would misleadingly appear to override that restrictive rule.
    """
    denied = rules.matching_rule(PermissionEffect.DENY, tool_name, tool_input)
    if denied:
        return AuthorizationResult(False, _rule_reason(denied))

    ask_rule = rules.matching_rule(PermissionEffect.ASK, tool_name, tool_input)
    must_ask = hook_ask or ask_rule is not None
    if not must_ask and _auto_allowed(tool_name, tool_input, hook_allow):
        return AuthorizationResult(True)
    requested_by = _ask_source(ask_rule) if ask_rule else None
    if hook_ask and requested_by is None:
        requested_by = "PreToolUse hook"
    if _prompt(
        tool_name,
        tool_input,
        one_time_only=must_ask,
        requested_by=requested_by,
    ):
        return AuthorizationResult(True)
    return AuthorizationResult(False, f"User declined to run {tool_name}.")


def confirm(tool_name: str, tool_input: dict, force: bool = False) -> bool:
    """Compatibility wrapper returning only :func:`authorize`'s boolean."""
    return authorize(tool_name, tool_input, hook_ask=force).allowed


def grant_skill_tools(entries: list) -> None:
    """Apply one Skill's allowed-tools entries for the rest of this turn."""
    for entry in entries:
        entry = str(entry).strip()
        match = _SKILL_BASH_RULE.match(entry)
        if match:
            pattern = match.group(1).strip()
            _SKILL_RULES.append((pattern, bash.compile_rule(pattern)))
        elif entry:
            tool_name = rules.normalize_tool_name(entry)
            if tool_name == "read":
                _SKILL_TOOLS.update(rules.READ_TOOLS)
            elif tool_name == "edit":
                _SKILL_TOOLS.update(rules.EDIT_TOOLS)
            elif tool_name == "*":
                _SKILL_TOOLS.update(GATED_TOOLS)
            elif tool_name:
                _SKILL_TOOLS.add(tool_name)
    if entries:
        ux.say(
            "skill grants (until your next message): " + ", ".join(entries),
            style=ux.S_INFO,
        )


def clear_skill_grants() -> None:
    """Expire all Skill grants at the next user prompt boundary."""
    _SKILL_RULES.clear()
    _SKILL_TOOLS.clear()


def reset() -> None:
    """Clear session grants, Skill grants, and the compiled settings cache."""
    _ALLOWED.clear()
    rules.reset_cache()
    clear_skill_grants()


def preload(tools) -> set:
    """Apply legacy whole-tool grants, excluding Bash and unknown tools."""
    applied = {
        tool
        for tool in tools
        if tool in GATED_TOOLS and tool not in NO_PRELOAD
    }
    _ALLOWED.update(applied)
    return applied
