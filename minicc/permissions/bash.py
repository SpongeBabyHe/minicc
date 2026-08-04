"""Pure Bash command analysis used by permission rules and approval prompts.

This module does not read settings, mutate session grants, or ask the user. It
tokenizes bounded shell syntax, recognizes Claude Code's read-only carve-out,
and evaluates already-parsed Bash allow or restrictive rules.
"""

import re
import shlex
from pathlib import Path

from .models import PermissionEffect, PermissionRule


_READONLY_CMDS = {
    "ls",
    "cat",
    "echo",
    "pwd",
    "head",
    "tail",
    "grep",
    "find",
    "wc",
    "which",
    "diff",
    "stat",
    "du",
}
_GIT_READONLY = {
    "status", "log", "diff", "show", "blame", "shortlog", "describe",
    "rev-parse", "ls-files", "ls-tree", "cat-file", "check-ignore",
}
_WRAPPERS = {
    "timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "noglob",
}
_OPERATORS = {"&&", "||", ";", "|", "|&", "&", ";;"}
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.S)
_FIND_MUTATING_FLAGS = {
    "-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprint0",
    "-fprintf", "-fprintf0", "-ok", "-okdir",
}
_NON_AUTOAPPROVABLE_WRAPPERS = {"flock", "ionice", "setsid", "watch"}
_UNSAFE_META = re.compile(r">|<|\$\(|`")
_HIDDEN_EXEC_META = re.compile(r"\$\(|`|[<>]\(")
_SAFE_REDIRECTS = re.compile(
    r"(?:\d*>&\d+|[&\d]*>\s*/dev/null)(?=$|\s|[;&|])"
)


def _separate_unquoted_newlines(command: str) -> str:
    """Turn unquoted shell newlines into command separators."""
    chars: list[str] = []
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            chars.append(char)
            escaped = True
            continue
        if char in ("'", '"'):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            chars.append(char)
            continue
        # Keep a real newline so shlex terminates a preceding ``#`` comment.
        chars.append("\n;\n" if char == "\n" and quote is None else char)
    return "".join(chars)


def _split_subcommands(command: str):
    """Return safely bounded token groups, or ``None`` for unsupported syntax."""
    if not command:
        return None
    command = _separate_unquoted_newlines(command)
    command = _SAFE_REDIRECTS.sub(" ", command)
    if _UNSAFE_META.search(command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    groups, current = [], []
    for token in tokens:
        if token in _OPERATORS:
            groups.append(current)
            current = []
        elif token in ("(", ")", "{", "}"):
            return None
        else:
            current.append(token)
    groups.append(current)
    return groups


def _split_restrictive_subcommands(command: str):
    """Best-effort split used only by deny and ask rules on unsafe shell."""
    if not command:
        return None
    try:
        lexer = shlex.shlex(
            _separate_unquoted_newlines(command),
            posix=True,
            punctuation_chars=True,
        )
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    groups, current = [], []
    for token in tokens:
        if token in _OPERATORS:
            groups.append(current)
            current = []
        elif token in ("(", ")", "{", "}"):
            return None
        else:
            current.append(token)
    groups.append(current)
    return groups


def _strip_wrappers(tokens: list, *, restrictive: bool = False) -> list:
    """Strip supported exec wrappers without hiding ambiguous options."""
    original = list(tokens)
    while tokens and tokens[0] in _WRAPPERS:
        wrapper = tokens.pop(0)
        if wrapper == "timeout":
            while tokens and tokens[0].startswith("-"):
                option = tokens.pop(0)
                if option == "--":
                    break
                if option in (
                    "--foreground",
                    "--preserve-status",
                    "--verbose",
                    "-v",
                ):
                    continue
                if option in ("--kill-after", "--signal", "-k", "-s"):
                    if not tokens:
                        return original
                    tokens.pop(0)
                    continue
                if re.fullmatch(r"(?:--kill-after|--signal)=.+|-[ks].+", option):
                    continue
                return original
            if not tokens or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", tokens[0]):
                return original
            tokens.pop(0)
        elif wrapper == "time":
            while tokens and tokens[0].startswith("-"):
                option = tokens.pop(0)
                if option == "--":
                    break
                if option in ("-p", "--portability"):
                    continue
                if restrictive and option in (
                    "-a",
                    "--append",
                    "-v",
                    "--verbose",
                    "--quiet",
                ):
                    continue
                if restrictive and option in (
                    "-f",
                    "--format",
                    "-o",
                    "--output",
                ):
                    if not tokens:
                        return original
                    tokens.pop(0)
                    continue
                if restrictive and re.fullmatch(
                    r"(?:--format|--output)=.+|-[fo].+",
                    option,
                ):
                    continue
                return original
        elif wrapper == "nice":
            if len(tokens) >= 2 and tokens[0] in ("-n", "--adjustment"):
                if not re.fullmatch(r"[+-]?\d+", tokens[1]):
                    return original
                del tokens[:2]
            elif tokens and re.fullmatch(r"(?:-n|--adjustment=)[+-]?\d+", tokens[0]):
                tokens.pop(0)
            elif tokens and re.fullmatch(r"-\d+", tokens[0]):
                tokens.pop(0)
            elif tokens and tokens[0].startswith("-"):
                return original
        elif wrapper == "stdbuf":
            while tokens and tokens[0].startswith("-"):
                option = tokens.pop(0)
                if option in ("-i", "-o", "-e"):
                    if not tokens:
                        return original
                    tokens.pop(0)
                elif not re.fullmatch(
                    r"-(?:i|o|e).+|--(?:input|output|error)=.+",
                    option,
                ):
                    return original
        elif wrapper == "command":
            if tokens and tokens[0] in ("-v", "-V"):
                return original
            if tokens and tokens[0] in ("-p", "--"):
                tokens.pop(0)
        elif wrapper in ("builtin", "nohup"):
            if tokens and tokens[0] == "--":
                tokens.pop(0)
            elif tokens and tokens[0].startswith("-"):
                return original
        if not tokens:
            return original
    if len(tokens) > 1 and tokens[0] == "xargs" and not tokens[1].startswith("-"):
        tokens.pop(0)
    return tokens


def _strip_env_assignments(tokens: list) -> list:
    while tokens and _ENV_ASSIGNMENT.fullmatch(tokens[0]):
        tokens.pop(0)
    return tokens


def _subcommand_is_readonly(tokens: list) -> bool:
    tokens = _strip_wrappers(list(tokens))
    if not tokens or "=" in tokens[0]:
        return False
    command = tokens[0]
    if command == "cd":
        if len(tokens) != 2:
            return False
        target = Path(tokens[1]).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        try:
            return target.resolve().is_relative_to(Path.cwd().resolve())
        except OSError:
            return False
    if command == "git":
        subcommand = next((token for token in tokens[1:] if not token.startswith("-")), None)
        return subcommand in _GIT_READONLY and not any(
            token.startswith("--output") for token in tokens
        )
    if command == "find":
        return not _FIND_MUTATING_FLAGS.intersection(tokens)
    return command in _READONLY_CMDS


def _mixes_cd_and_git(groups: list[list]) -> bool:
    has_git = False
    changes_directory = False
    cwd = Path.cwd().resolve()
    for group in groups:
        tokens = _strip_wrappers(list(group))
        if not tokens or "=" in tokens[0]:
            continue
        if tokens[0] == "git":
            has_git = True
        elif tokens[0] == "cd" and len(tokens) == 2:
            target = Path(tokens[1]).expanduser()
            if not target.is_absolute():
                target = cwd / target
            try:
                changes_directory = changes_directory or target.resolve() != cwd
            except OSError:
                changes_directory = True
    return has_git and changes_directory


def is_readonly_command(command: str) -> bool:
    """Whether every bounded subcommand belongs to the read-only carve-out."""
    groups = _split_subcommands(command)
    return (
        groups is not None
        and not _mixes_cd_and_git(groups)
        and all(_subcommand_is_readonly(group) for group in groups)
    )


def _glob_to_re(pattern: str) -> str:
    return ".*".join(re.escape(part) for part in pattern.split("*"))


def compile_rule(pattern: str) -> re.Pattern:
    """Compile Claude Code's Bash rule wildcard and trailing-prefix syntax."""
    pattern = pattern.strip()
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    if pattern.endswith(" *"):
        return re.compile(f"^{_glob_to_re(pattern[:-2])}( .*)?$")
    return re.compile(f"^{_glob_to_re(pattern)}$")


def _bash_rule_matches_group(rule: PermissionRule, tokens: list) -> bool:
    if rule.argument_pattern is None:
        return True
    tokens = _strip_wrappers(
        list(tokens),
        restrictive=rule.effect != PermissionEffect.ALLOW,
    )
    if rule.effect != PermissionEffect.ALLOW:
        tokens = _strip_env_assignments(tokens)
    command = " ".join(tokens)
    return bool(command and rule.compiled and rule.compiled.match(command))


def rule_matches_command(rule: PermissionRule, command: str) -> bool:
    """Match one parsed Bash rule against a complete compound command."""
    if rule.argument_pattern is None:
        return True
    groups = _split_subcommands(command)
    if groups is None:
        if rule.effect == PermissionEffect.ALLOW:
            return False
        if _HIDDEN_EXEC_META.search(command):
            return True
        groups = _split_restrictive_subcommands(command)
        if groups is None:
            return True
    return any(_bash_rule_matches_group(rule, group) for group in groups)


def _allow_pattern_matches(
    pattern: str,
    compiled: re.Pattern,
    tokens: list,
) -> bool:
    command = " ".join(tokens)
    if not command:
        return False
    if tokens[0] == "find" and _FIND_MUTATING_FLAGS.intersection(tokens):
        return "*" not in pattern and bool(compiled.match(command))
    if tokens[0] in _NON_AUTOAPPROVABLE_WRAPPERS and "*" in pattern:
        return False
    return bool(compiled.match(command))


def _subcommand_matches_rule(
    tokens: list,
    setting_rules: list[PermissionRule],
    skill_rules: list[tuple[str, re.Pattern]],
) -> bool:
    tokens = _strip_wrappers(list(tokens))
    if not tokens or "=" in tokens[0]:
        return False
    return any(
        rule.effect == PermissionEffect.ALLOW
        and rule.tool_pattern == "bash"
        and rule.argument_pattern is not None
        and rule.compiled
        and _allow_pattern_matches(rule.argument_pattern, rule.compiled, tokens)
        for rule in setting_rules
    ) or any(
        _allow_pattern_matches(pattern, regex, tokens)
        for pattern, regex in skill_rules
    )


def command_is_allowed(
    command: str,
    setting_rules: list[PermissionRule],
    skill_rules: list[tuple[str, re.Pattern]],
) -> bool:
    """Whether built-in, persisted, or Skill Bash grants cover every subcommand."""
    if any(
        rule.effect == PermissionEffect.ALLOW
        and rule.tool_pattern == "bash"
        and (
            rule.argument_pattern is None
            or rule.argument_pattern.strip() == "*"
        )
        for rule in setting_rules
    ):
        return True
    groups = _split_subcommands(command)
    return groups is not None and not _mixes_cd_and_git(groups) and all(
        _subcommand_is_readonly(group)
        or _subcommand_matches_rule(group, setting_rules, skill_rules)
        for group in groups
    )


def derive_rules(
    command: str,
    setting_rules: list[PermissionRule],
    skill_rules: list[tuple[str, re.Pattern]],
) -> list[str]:
    """Derive at most five bounded Bash rules suitable for an ``always`` grant."""
    groups = _split_subcommands(command)
    if groups is None:
        return []
    derived: list[str] = []
    for group in groups:
        if _subcommand_is_readonly(group) or _subcommand_matches_rule(
            group,
            setting_rules,
            skill_rules,
        ):
            continue
        tokens = _strip_wrappers(list(group))
        if not tokens or "=" in tokens[0]:
            return []
        rule = (
            " ".join(tokens[:2]) + " *"
            if len(tokens) > 2
            else " ".join(tokens)
        )
        if rule not in derived:
            derived.append(rule)
    return derived[:5]
