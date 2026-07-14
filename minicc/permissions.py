# Trust model for minicc:
#   - Reads (read_file, glob, grep) are not gated. The model can see anything
#     this process can see. Don't run minicc on machines with other people's data.
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


import re
import shlex
import sys
from pathlib import Path
from minicc import config, ux

# Gated tools: these require user approval before use. Single source of truth for
# "which tools gate" — both confirm() and preload() key off it.
# web_fetch gates for the same reason bash does: a network request's effect is
# unknown in advance, and under prompt injection a crafted URL is a data-
# exfiltration channel (secrets in query params). The user sees each URL.
GATED_TOOLS = ["bash", "write_file", "edit_file", "memory", "web_fetch"]

# Multi-command tools gate only *some* commands: a tool listed here gates only the
# named commands, its others are free. (memory: writes gate; `view` stays free so the
# model can always check memory.) Tools not listed gate every call.
_GATED_COMMANDS = {"memory": {"create", "str_replace", "delete"}}

# Gated tools that may NOT be pre-approved from config (preload). bash's effect is
# unbounded + irreversible and the gate is its ONLY boundary, so trusting it must
# stay a per-session decision, never a persistent settings entry. See PERMISSIONS.md.
NO_PRELOAD = {"bash"}

# session-scoped allowed tools if user answers "all" to the prompt
_ALLOWED = set()


# ─── Read-only bash carve-out (CC parity) ────────────────────────────────────
# CC runs a built-in set of read-only commands "without a permission prompt in
# every mode" (official permissions doc; list mirrored verbatim, cd/git handled
# specially below). A compound command qualifies only if EVERY subcommand does —
# `ls && rm x` prompts; CC splits on the same operators for the same reason.
# Everything here FAILS SAFE: what we can't parse or don't recognize just prompts.
# Dogfood evidence: the three-arm /init run showed 18 approval prompts on the
# baseline vs 0 on CC — and prompting on every `ls` suppresses exploration.

_READONLY_CMDS = {
    "ls", "cat", "echo", "pwd", "head", "tail", "grep", "find", "wc",
    "which", "diff", "stat", "du",
}
# git subcommands whose every form is read-only. Deliberately EXCLUDES branch/tag/
# remote/stash/reflog (each has mutating forms — branch -D, stash pop, reflog expire).
_GIT_READONLY = {
    "status", "log", "diff", "show", "blame", "shortlog", "describe",
    "rev-parse", "ls-files", "ls-tree", "cat-file", "check-ignore",
}
# process wrappers CC strips before matching (timeout 30 npm test → npm test)
_WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf"}
_OPERATORS = {"&&", "||", ";", "|", "|&", "&", ";;"}
# metacharacters that can smuggle a write or an exec past the whitelist:
# redirections (git log > f), command/process substitution (cat $(rm x), <(...)),
# backticks. Rejection only means: prompt as before.
_UNSAFE_META = re.compile(r">|<|\$\(|`")
# ...except the two ubiquitous HARMLESS redirect forms: fd duplication (2>&1) and
# the /dev/null sink. Dogfood R2: leaving these in the unsafe set false-prompted
# ordinary exploration AND suppressed the `always` offer. Stripped before the
# guard; any other redirect (`> file`, `2>err.log`) still disqualifies.
_SAFE_REDIRECTS = re.compile(r"\d*>&\d+|[&\d]*>\s*/dev/null")


def _split_subcommands(command: str):
    """Split a command into subcommand token groups on CC's operators, or None if
    it can't be safely bounded (newlines, redirections, substitutions, subshells,
    or anything shlex can't tokenize → the caller just prompts, as always)."""
    if not command or "\n" in command:
        return None
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
        elif t in ("(", ")"):
            return None  # subshells: out of scope, prompt
        else:
            cur.append(t)
    groups.append(cur)
    return groups


def _strip_wrappers(toks: list) -> list:
    while toks and toks[0] in _WRAPPERS:
        toks.pop(0)
        # drop the wrapper's own flags/duration (timeout -k 5 10s CMD)
        while toks and (toks[0].startswith("-") or re.fullmatch(r"\d+[smhd]?", toks[0])):
            toks.pop(0)
    # bare xargs only (xargs -n1 ... is matched as xargs itself, per CC)
    if len(toks) > 1 and toks[0] == "xargs" and not toks[1].startswith("-"):
        toks.pop(0)
    return toks


def _subcommand_is_readonly(toks: list) -> bool:
    toks = _strip_wrappers(list(toks))
    if not toks or "=" in toks[0]:  # empty, or env-var prefix (PATH=/evil ls) — prompt
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
        banned = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls"}
        return not banned.intersection(toks)
    return cmd in _READONLY_CMDS


def is_readonly_command(command: str) -> bool:
    """True iff every subcommand of `command` is in the read-only carve-out, so the
    call may skip the permission prompt."""
    groups = _split_subcommands(command)
    return groups is not None and all(_subcommand_is_readonly(g) for g in groups)


# ─── Persisted allow rules (CC's permission rules, bash subset) ──────────────
# CC: `Bash(npm test *)` in permissions.allow auto-approves matching commands;
# "Yes, don't ask again" persists such a rule per project + command prefix. The
# wildcard semantics are CC's, verbatim: `*` matches any sequence including
# spaces; a trailing " *" (or its ":*" alias) enforces a word boundary — `ls *`
# matches `ls -la` and bare `ls` but not `lsof`; `ls*` matches both. Wrappers are
# stripped before matching, and a compound command qualifies only if EVERY
# subcommand is read-only or rule-matched.

_RULES: list | None = None  # [(pattern, compiled)] — lazy; reset() reloads


def _glob_to_re(s: str) -> str:
    return ".*".join(re.escape(part) for part in s.split("*"))


def _compile_rule(pattern: str):
    pattern = pattern.strip()
    if pattern.endswith(":*"):  # `ls:*` is CC's alias for `ls *`
        pattern = pattern[:-2] + " *"
    if pattern.endswith(" *"):
        return re.compile(f"^{_glob_to_re(pattern[:-2])}( .*)?$")
    return re.compile(f"^{_glob_to_re(pattern)}$")


def _rules() -> list:
    global _RULES
    if _RULES is None:
        _RULES = [(p, _compile_rule(p)) for p in config.permission_allow_rules()]
    return _RULES


def _subcommand_matches_rule(toks: list) -> bool:
    toks = _strip_wrappers(list(toks))
    if not toks or "=" in toks[0]:
        return False
    cmd = " ".join(toks)
    return any(rx.match(cmd) for _p, rx in _rules())


def _bash_allowed(command: str) -> bool:
    """Whether a bash call skips the prompt: every subcommand is read-only
    (built-in carve-out) or matches a persisted allow rule."""
    groups = _split_subcommands(command)
    return groups is not None and all(
        _subcommand_is_readonly(g) or _subcommand_matches_rule(g) for g in groups
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


def _is_gated(tool_name: str, tool_input: dict) -> bool:
    """Whether THIS call needs approval. A tool gates iff its name is in GATED_TOOLS;
    a multi-command tool in _GATED_COMMANDS gates only those commands (so `memory
    view` is free while `memory create`/`str_replace` prompt); bash skips the gate
    for read-only commands and persisted allow rules (CC's carve-outs)."""
    if tool_name not in GATED_TOOLS:
        return False
    if tool_name == "bash" and _bash_allowed(tool_input.get("command", "")):
        return False
    gated_cmds = _GATED_COMMANDS.get(tool_name)
    if gated_cmds is not None:
        return tool_input.get("command") in gated_cmds
    return True


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
        ux.say("(empty answer ignored — type yes, no, all, or always)", style=ux.S_INFO)


def confirm(tool_name: str, tool_input: dict, force: bool = False) -> bool:
    """Whether this tool call may run. `force` (a PreToolUse hook's "ask") prompts
    even for a normally-free tool and ignores the session allowlist — the hook is
    explicitly asking the user to confirm this specific call.

    For bash, `always` persists a narrow prefix rule per subcommand (CC's "Yes,
    don't ask again") to project settings — future matching commands skip the
    prompt. Offered only when the command can be safely bounded into rules."""
    if not force:
        if not _is_gated(tool_name, tool_input):
            return True
        if tool_name in _ALLOWED:
            return True
    ux.say(_format_args(tool_name, tool_input))
    save_rules = derive_rules(tool_input.get("command", "")) if tool_name == "bash" else []
    options = "[yes/no/all]"
    if save_rules:
        ux.say(
            f"always = don't ask again for: {', '.join(save_rules)}  (saved to project settings)",
            style=ux.S_INFO,
        )
        options = "[yes/no/all/always]"
    answer = _read_answer(f"Approve? {options}: ")
    if answer == "all":
        _ALLOWED.add(tool_name)
        return True
    if answer == "always" and save_rules:
        for rule in save_rules:
            config.add_allow_rule(rule)
            _rules().append((rule, _compile_rule(rule)))  # live for this session too
        return True
    return answer == "yes"


def reset():
    """Clear the session-scoped allowed-tools set and drop the cached allow rules
    (re-read from settings on next use). Called by /clear."""
    global _RULES
    _ALLOWED.clear()
    _RULES = None


def preload(tools) -> set:
    """Pre-approve gated tools from config at startup (re-applied after /clear).
    Excludes NO_PRELOAD (bash) and non-gated names. Returns the set applied, so
    the caller can surface which tools now skip the prompt."""
    applied = {t for t in tools if t in GATED_TOOLS and t not in NO_PRELOAD}
    _ALLOWED.update(applied)
    return applied
