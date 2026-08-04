"""Source-aware authorization for local tool calls.

Settings rules resolve in ``deny -> ask -> allow`` order before built-in,
session, Skill, or Hook grants. Bash receives additional command parsing so a
compound call is silent only when every subcommand is read-only or explicitly
allowed. This module decides whether a call may run; it does not sandbox the
handler after approval.
"""

# Trust model for minicc:
#   - Reads (read_file, glob, grep) are free by default, but settings ask/deny
#     rules can restrict them. The process itself still has the user's OS access.
#   - Writes (write_file, edit_file) are gated regardless of whether they
#     create or overwrite — the tool can't tell the difference in advance.
#   - memory: writes (create/str_replace) are gated like write_file; `memory view`
#     is NOT — the model checks memory constantly and reads are safe.
#   - bash is gated because we don't know what it will do — EXCEPT a read-only
#     command whitelist (ls/cat/grep/git status…, CC's built-in carve-out) which
#     runs promptless, and persisted `bash(prefix *)` allow rules (CC's
#     permission rules). NOTE: 'a'-approving bash effectively disables all gating
#     for network calls, package installs, git push, kill, etc. Use 'y' or a
#     narrow `always` rule; reserve 'a' for write_file/edit_file.
#   - Scope escapes (paths outside cwd) are NOT detected. Add later if needed.
#
# Full trust model + permission-layer vs execution-layer analysis: PERMISSIONS.md


import fnmatch
import os
import re
import shlex
import sys
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from pathlib import Path, PurePosixPath
from minicc import config, ux

# Gated tools require user approval unless policy or a scoped grant allows them.
# Both authorize() and preload() key off this list.
# web_fetch gates for the same reason bash does: a network request's effect is
# unknown in advance, and under prompt injection a crafted URL is a data-
# exfiltration channel (secrets in query params). The user sees each URL.
GATED_TOOLS = ["bash", "write_file", "edit_file", "memory", "web_fetch"]

# Multi-command tools gate only *some* commands: a tool listed here gates only the
# named commands, its others are free. (memory: writes gate; `view` stays free so the
# model can always check memory.) Tools not listed gate every call.
_GATED_COMMANDS = {"memory": {"create", "str_replace", "delete"}}

# Gated tools that legacy whole-tool ``allowed_tools`` may not pre-approve.
# Persistent Bash grants must use a narrow ``permissions.allow`` command rule.
NO_PRELOAD = {"bash"}

# session-scoped allowed tools if user answers "all" to the prompt
_ALLOWED = set()


class PermissionEffect(str, Enum):
    """The three CC permission-rule outcomes, in precedence order."""

    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


@dataclass(frozen=True)
class PermissionRule:
    """One parsed settings rule with the source needed for correct resolution."""

    effect: PermissionEffect
    raw: str
    tool_pattern: str
    argument_pattern: str | None
    source: config.SettingsSource
    compiled: re.Pattern | None = field(default=None, repr=False, compare=False)

    @property
    def anchor(self) -> Path:
        return self.source.anchor


@dataclass(frozen=True)
class AuthorizationResult:
    """Final decision for one tool call after policy and optional user review."""

    allowed: bool
    reason: str | None = None


# ─── Read-only bash carve-out (CC parity) ────────────────────────────────────
# CC runs a built-in set of read-only commands "without a permission prompt in
# every mode" (official permissions doc; list mirrored verbatim, cd/git handled
# specially below). A compound command qualifies only if EVERY subcommand does —
# `ls && rm x` prompts; CC splits on the same operators for the same reason.
# Everything here FAILS SAFE: what we can't parse or don't recognize just prompts.
# Dogfood evidence: the three-arm /init run showed 18 approval prompts on the
# baseline vs 0 on CC — and prompting on every `ls` suppresses exploration.

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
# git subcommands whose every form is read-only. Deliberately EXCLUDES branch/tag/
# remote/stash/reflog (each has mutating forms — branch -D, stash pop, reflog expire).
_GIT_READONLY = {
    "status", "log", "diff", "show", "blame", "shortlog", "describe",
    "rev-parse", "ls-files", "ls-tree", "cat-file", "check-ignore",
}
# process wrappers CC strips before matching (timeout 30 npm test → npm test)
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
# metacharacters that can smuggle a write or an exec past the whitelist:
# redirections (git log > f), command/process substitution (cat $(rm x), <(...)),
# backticks. Rejection only means: prompt as before.
_UNSAFE_META = re.compile(r">|<|\$\(|`")
_HIDDEN_EXEC_META = re.compile(r"\$\(|`|[<>]\(")
# ...except the two ubiquitous HARMLESS redirect forms: fd duplication (2>&1) and
# the /dev/null sink. Dogfood R2: leaving these in the unsafe set false-prompted
# ordinary exploration AND suppressed the `always` offer. Stripped before the
# guard; any other redirect (`> file`, `2>err.log`) still disqualifies.
_SAFE_REDIRECTS = re.compile(
    r"(?:\d*>&\d+|[&\d]*>\s*/dev/null)(?=$|\s|[;&|])"
)


def _separate_unquoted_newlines(command: str) -> str:
    """Turn shell newlines into separators while preserving quoted newlines."""
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
        # Keep a real newline before the separator so shlex ends any ``#``
        # comment there instead of swallowing the following command.
        chars.append("\n;\n" if char == "\n" and quote is None else char)
    return "".join(chars)


def _split_subcommands(command: str):
    """Split a command into subcommand token groups on CC's operators, or None if
    it can't be safely bounded (newlines, redirections, substitutions, subshells,
    or anything shlex can't tokenize → the caller just prompts, as always)."""
    if not command:
        return None
    command = _separate_unquoted_newlines(command)
    command = _SAFE_REDIRECTS.sub(" ", command)
    if _UNSAFE_META.search(command):
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    groups, cur = [], []
    for t in toks:
        if t in _OPERATORS:
            groups.append(cur)
            cur = []
        elif t in ("(", ")", "{", "}"):
            return None  # subshells: out of scope, prompt
        else:
            cur.append(t)
    groups.append(cur)
    return groups


def _split_restrictive_subcommands(command: str):
    """Best-effort split used only by deny/ask rules on otherwise unsafe shell."""
    if not command:
        return None
    try:
        lex = shlex.shlex(
            _separate_unquoted_newlines(command),
            posix=True,
            punctuation_chars=True,
        )
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    groups, current = [], []
    for token in toks:
        if token in _OPERATORS:
            groups.append(current)
            current = []
        elif token in ("(", ")", "{", "}"):
            return None
        else:
            current.append(token)
    groups.append(current)
    return groups


def _strip_wrappers(toks: list, *, restrictive: bool = False) -> list:
    """Strip CC's exec wrappers; restrictive rules also see through safe options."""
    original = list(toks)
    while toks and toks[0] in _WRAPPERS:
        wrapper = toks.pop(0)
        if wrapper == "timeout":
            while toks and toks[0].startswith("-"):
                option = toks.pop(0)
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
                    if not toks:
                        return original
                    toks.pop(0)
                    continue
                if re.fullmatch(
                    r"(?:--kill-after|--signal)=.+|-[ks].+",
                    option,
                ):
                    continue
                return original
            if not toks or not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", toks[0]):
                return original
            toks.pop(0)
        elif wrapper == "time":
            while toks and toks[0].startswith("-"):
                option = toks.pop(0)
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
                    if not toks:
                        return original
                    toks.pop(0)
                    continue
                if restrictive and re.fullmatch(
                    r"(?:--format|--output)=.+|-[fo].+",
                    option,
                ):
                    continue
                return original
        elif wrapper == "nice":
            if len(toks) >= 2 and toks[0] in ("-n", "--adjustment"):
                if not re.fullmatch(r"[+-]?\d+", toks[1]):
                    return original
                del toks[:2]
            elif toks and re.fullmatch(r"(?:-n|--adjustment=)[+-]?\d+", toks[0]):
                toks.pop(0)
            elif toks and re.fullmatch(r"-\d+", toks[0]):
                toks.pop(0)
            elif toks and toks[0].startswith("-"):
                return original
        elif wrapper == "stdbuf":
            while toks and toks[0].startswith("-"):
                option = toks.pop(0)
                if option in ("-i", "-o", "-e"):
                    if not toks:
                        return original
                    toks.pop(0)
                elif not re.fullmatch(r"-(?:i|o|e).+|--(?:input|output|error)=.+", option):
                    return original
        elif wrapper == "command":
            if toks and toks[0] in ("-v", "-V"):
                return original
            if toks and toks[0] in ("-p", "--"):
                toks.pop(0)
        elif wrapper in ("builtin", "nohup"):
            if toks and toks[0] == "--":
                toks.pop(0)
            elif toks and toks[0].startswith("-"):
                return original
        # noglob has no wrapper arguments.
        if not toks:
            return original
    # bare xargs only (xargs -n1 ... is matched as xargs itself, per CC)
    if len(toks) > 1 and toks[0] == "xargs" and not toks[1].startswith("-"):
        toks.pop(0)
    return toks


def _strip_env_assignments(toks: list) -> list:
    while toks and _ENV_ASSIGNMENT.fullmatch(toks[0]):
        toks.pop(0)
    return toks


def _subcommand_is_readonly(toks: list) -> bool:
    toks = _strip_wrappers(list(toks))
    # empty, or env-var prefix (PATH=/evil ls) — prompt
    if not toks or "=" in toks[0]:
        return False
    cmd = toks[0]
    if cmd == "cd":
        # cd inside the working directory is read-only (CC rule); outside prompts.
        if len(toks) != 2:
            return False
        target = Path(toks[1]).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        try:
            return target.resolve().is_relative_to(Path.cwd().resolve())
        except OSError:
            return False
    if cmd == "git":
        sub = next((t for t in toks[1:] if not t.startswith("-")), None)
        return sub in _GIT_READONLY and not any(
            t.startswith("--output") for t in toks  # git log --output=f writes
        )
    if cmd == "find":
        # find is read-only EXCEPT its exec/mutate flags (CC prompts for these too)
        return not _FIND_MUTATING_FLAGS.intersection(toks)
    return cmd in _READONLY_CMDS


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
    """True if every subcommand of `command` is in the read-only carve-out, so the
    call may skip the permission prompt."""
    groups = _split_subcommands(command)
    return (
        groups is not None
        and not _mixes_cd_and_git(groups)
        and all(_subcommand_is_readonly(group) for group in groups)
    )


# ─── Source-aware permission rules ──────────────────────────────────────────

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
_READ_TOOLS = frozenset({"read_file", "glob", "grep"})
_EDIT_TOOLS = frozenset({"edit_file", "write_file"})
_READ_DENY_EDIT_TOOLS = frozenset({"edit_file"})
_SERVER_TOOLS = frozenset({"web_search"})
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

# Grants from an invoked skill's allowed-tools frontmatter. Active until the
# next user prompt (CC scopes grants to "while the skill is active"; its
# disallowed-tools doc pins that window: "clears when you send your next
# message"). bash(...) entries become temporary allow rules; bare names ungate
# that tool outright. Never persisted. See SKILL_DESIGN.md.
_SKILL_RULES: list[tuple[str, re.Pattern]] = []
_SKILL_TOOLS: set = set()  # lowercase tool names


def _glob_to_re(s: str) -> str:
    return ".*".join(re.escape(part) for part in s.split("*"))


def _compile_rule(pattern: str):
    pattern = pattern.strip()
    if pattern.endswith(":*"):  # `ls:*` is CC's alias for `ls *`
        pattern = pattern[:-2] + " *"
    if pattern.endswith(" *"):
        return re.compile(f"^{_glob_to_re(pattern[:-2])}( .*)?$")
    return re.compile(f"^{_glob_to_re(pattern)}$")


def _normalize_tool_name(name: str) -> str | None:
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
    tool_pattern = _normalize_tool_name(match.group(1))
    if tool_pattern is None:
        return None
    if effect == PermissionEffect.ALLOW and "*" in tool_pattern:
        return None
    argument_pattern = match.group(2)
    compiled = (
        _compile_rule(argument_pattern)
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


def permission_rules() -> list[PermissionRule]:
    """Compile effective rules while retaining source and path anchor.

    Project ``deny`` and ``ask`` rules remain effective in a restricted
    workspace because they only reduce authority. Project ``allow`` rules enter
    through the active view only after Workspace Trust.
    """
    global _SETTINGS_RULES, _SETTINGS_RULES_VIEW
    view = config.current_settings()
    if _SETTINGS_RULES is None or _SETTINGS_RULES_VIEW is not view:
        rules: list[PermissionRule] = []
        for effect in (PermissionEffect.DENY, PermissionEffect.ASK):
            for entry in view.snapshot.entries(("permissions", effect.value)):
                if rule := _parse_rule(entry, effect):
                    rules.append(rule)
        for entry in view.entries(("permissions", PermissionEffect.ALLOW.value)):
            if rule := _parse_rule(entry, PermissionEffect.ALLOW):
                rules.append(rule)
        _SETTINGS_RULES = rules
        _SETTINGS_RULES_VIEW = view
    return _SETTINGS_RULES


def _tool_matches(rule: PermissionRule, tool_name: str) -> bool:
    if rule.tool_pattern == "read":
        covered = _READ_TOOLS
        if rule.effect == PermissionEffect.DENY:
            covered = covered | _READ_DENY_EDIT_TOOLS
        return tool_name in covered
    if rule.tool_pattern == "edit":
        return tool_name in _EDIT_TOOLS
    return fnmatch.fnmatchcase(tool_name, rule.tool_pattern)


def _path_pattern(rule: PermissionRule) -> str:
    pattern = rule.argument_pattern or ""
    if pattern.startswith("//"):
        return os.path.normpath("/" + pattern[2:])
    if pattern.startswith("~/"):
        return os.path.normpath(str(Path.home() / pattern[2:]))
    if pattern.startswith("/"):
        return os.path.normpath(str(rule.anchor / pattern[1:]))
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
    """Match a path glob where ``*`` stays within one directory and ``**`` recurses."""
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
    """Match CC WebFetch domains without letting ``*`` cross a dot."""
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
    """Match restrictive ``param:value`` syntax, or return None if not applicable."""
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
    if not _tool_matches(rule, tool_name):
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
    groups = _split_subcommands(str(tool_input.get("command", "")))
    if groups is None:
        if rule.effect == PermissionEffect.ALLOW:
            return False
        command = str(tool_input.get("command", ""))
        if _HIDDEN_EXEC_META.search(command):
            return True
        groups = _split_restrictive_subcommands(command)
        if groups is None:
            return True
    return any(_bash_rule_matches_group(rule, group) for group in groups)


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


def _matching_rule(
    effect: PermissionEffect,
    tool_name: str,
    tool_input: dict,
) -> PermissionRule | None:
    return next(
        (
            rule
            for rule in permission_rules()
            if rule.effect == effect and _rule_matches_call(rule, tool_name, tool_input)
        ),
        None,
    )


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


def _subcommand_matches_rule(toks: list) -> bool:
    toks = _strip_wrappers(list(toks))
    if not toks or "=" in toks[0]:
        return False
    setting_rules = (
        rule
        for rule in permission_rules()
        if rule.effect == PermissionEffect.ALLOW
        and rule.tool_pattern == "bash"
        and rule.argument_pattern is not None
    )
    return any(
        rule.compiled
        and _allow_pattern_matches(rule.argument_pattern or "", rule.compiled, toks)
        for rule in setting_rules
    ) or any(
        _allow_pattern_matches(pattern, regex, toks)
        for pattern, regex in _SKILL_RULES
    )


def _bash_allowed(command: str) -> bool:
    """Whether a bash call skips the prompt: every subcommand is read-only
    (built-in carve-out) or matches a persisted allow rule."""
    if any(
        rule.effect == PermissionEffect.ALLOW
        and rule.tool_pattern == "bash"
        and (
            rule.argument_pattern is None
            or rule.argument_pattern.strip() == "*"
        )
        for rule in permission_rules()
    ):
        return True
    groups = _split_subcommands(command)
    return groups is not None and not _mixes_cd_and_git(groups) and all(
        _subcommand_is_readonly(group) or _subcommand_matches_rule(group)
        for group in groups
    )


def derive_rules(command: str) -> list:
    """The rule patterns `always` would persist for this command: for each
    subcommand not already free, the first two tokens + " *" (an exact rule when
    the command is that short). Mirrors the rule shapes CC's own dialog writes
    (`uv run *`, exact single commands). Empty when the command can't be safely
    bounded — `always` isn't offered then."""
    groups = _split_subcommands(command)
    if groups is None:
        return []
    rules = []
    for g in groups:
        if _subcommand_is_readonly(g) or _subcommand_matches_rule(g):
            continue
        toks = _strip_wrappers(list(g))
        if not toks or "=" in toks[0]:
            return []  # can't bound an env-prefixed command — no always
        rule = " ".join(toks[:2]) + " *" if len(toks) > 2 else " ".join(toks)
        if rule not in rules:
            rules.append(rule)
    return rules[:5]  # CC caps rules saved per compound command at 5


def _requires_prompt_by_default(tool_name: str, tool_input: dict) -> bool:
    if tool_name not in GATED_TOOLS:
        return False
    gated_cmds = _GATED_COMMANDS.get(tool_name)
    if gated_cmds is not None:
        return tool_input.get("command") in gated_cmds
    return True


def _auto_allowed(tool_name: str, tool_input: dict, hook_allow: bool = False) -> bool:
    if hook_allow or tool_name in _ALLOWED or tool_name in _SKILL_TOOLS:
        return True
    if tool_name == "bash":
        return _bash_allowed(str(tool_input.get("command", "")))
    if _matching_rule(PermissionEffect.ALLOW, tool_name, tool_input):
        return True
    return not _requires_prompt_by_default(tool_name, tool_input)


def _is_gated(tool_name: str, tool_input: dict) -> bool:
    """Compatibility query: whether this call is not currently auto-authorized.

    A policy denial also returns true: callers using this helper are asking
    whether the call may proceed silently, not whether a prompt will definitely
    be shown.
    """
    if _matching_rule(PermissionEffect.DENY, tool_name, tool_input):
        return True
    if _matching_rule(PermissionEffect.ASK, tool_name, tool_input):
        return True
    return not _auto_allowed(tool_name, tool_input)


def _matches_all_uses(rule: PermissionRule) -> bool:
    if rule.argument_pattern is None:
        return True
    if rule.tool_pattern == "bash" and rule.argument_pattern.strip() == "*":
        return True
    return (
        rule.tool_pattern == "web_fetch"
        and rule.argument_pattern.casefold().strip() == "domain:*"
    )


def filter_tools(tools: list[dict]) -> list[dict]:
    """Remove tools disabled for every call before advertising them.

    ``web_search`` executes inside the Messages API, so minicc cannot pause at
    its individual invocation. Any ``deny`` or ``ask`` rule for that server tool
    therefore fails closed by hiding it instead of bypassing scoped policy.
    """
    rules = permission_rules()
    blocking = [rule for rule in rules if _matches_all_uses(rule)]
    filtered = [
        tool
        for tool in tools
        if not any(
            _tool_matches(rule, tool["name"])
            and (
                rule.effect == PermissionEffect.DENY
                or (
                    rule.effect == PermissionEffect.ASK
                    and tool["name"] in _SERVER_TOOLS
                )
            )
            for rule in blocking
        )
        and not (
            tool["name"] in _SERVER_TOOLS
            and any(
                rule.effect in (PermissionEffect.DENY, PermissionEffect.ASK)
                and _tool_matches(rule, tool["name"])
                for rule in rules
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
    """Read an approval answer safely. Two guards, both from a dogfood phantom
    decline (a write_file was 'declined' by an answer the user never typed):
    (1) flush stale buffered stdin first — keystrokes typed while the model was
    streaming would otherwise be consumed AS the answer; (2) an empty answer
    re-prompts — a stray Enter must never decide a permission."""
    try:
        import termios

        if sys.stdin.isatty():
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass  # non-tty (tests/pipes) or non-Unix: nothing to flush
    while True:
        answer = input(prompt).strip().lower()
        if answer:
            return answer
        ux.say("(empty answer ignored — type yes, no, all, or always)",
               style=ux.S_INFO)


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
            and config.current_settings().trusted
        )
        else []
    )
    options = "[yes/no]" if one_time_only else "[yes/no/all]"
    if save_rules and not one_time_only:
        ux.say(
            f"always = don't ask again for: {', '.join(save_rules)}  (saved to local settings)",
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
    matching settings ``deny`` or ``ask`` rule. A forced prompt is deliberately
    one-shot: session-wide and persistent choices would not override the rule on
    the next call and would therefore be misleading.
    """
    denied = _matching_rule(PermissionEffect.DENY, tool_name, tool_input)
    if denied:
        return AuthorizationResult(False, _rule_reason(denied))

    ask_rule = _matching_rule(PermissionEffect.ASK, tool_name, tool_input)
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
    """Compatibility wrapper around :func:`authorize`."""
    return authorize(tool_name, tool_input, hook_ask=force).allowed


_SKILL_BASH_RULE = re.compile(r"(?is)^bash\((.+)\)$")


def grant_skill_tools(entries: list):
    """Apply an invoked skill's allowed-tools for the rest of the turn.
    `bash(git add *)` shapes use the same rule compiler as persisted rules
    (case-insensitive on the tool name, CC-export friendly); bare names ungate
    that tool. The grant is printed so temporary trust stays visible — the same
    principle as the startup allow-rules line."""
    for e in entries:
        e = str(e).strip()
        m = _SKILL_BASH_RULE.match(e)
        if m:
            pat = m.group(1).strip()
            _SKILL_RULES.append((pat, _compile_rule(pat)))
        elif e:
            if tool_name := _normalize_tool_name(e):
                if tool_name == "read":
                    _SKILL_TOOLS.update(_READ_TOOLS)
                elif tool_name == "edit":
                    _SKILL_TOOLS.update(_EDIT_TOOLS)
                elif tool_name == "*":
                    _SKILL_TOOLS.update(GATED_TOOLS)
                else:
                    _SKILL_TOOLS.add(tool_name)
    if entries:
        ux.say(
            "skill grants (until your next message): " + ", ".join(entries),
            style=ux.S_INFO,
        )


def clear_skill_grants():
    """Called on every new user prompt: skill grants don't outlive the turn."""
    _SKILL_RULES.clear()
    _SKILL_TOOLS.clear()


def reset():
    """Clear the session-scoped allowed-tools set and drop the cached allow rules
    (re-read from settings on next use). Called by /clear."""
    global _SETTINGS_RULES, _SETTINGS_RULES_VIEW
    _ALLOWED.clear()
    _SETTINGS_RULES = None
    _SETTINGS_RULES_VIEW = None
    clear_skill_grants()


def preload(tools) -> set:
    """Pre-approve gated tools from config at startup (re-applied after /clear).
    Excludes NO_PRELOAD (bash) and non-gated names. Returns the set applied, so
    the caller can surface which tools now skip the prompt."""
    applied = {t for t in tools if t in GATED_TOOLS and t not in NO_PRELOAD}
    _ALLOWED.update(applied)
    return applied
