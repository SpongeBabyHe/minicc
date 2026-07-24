"""Skills — packaged instructions loaded on demand (CC's skills system).

CC's model (official docs, code.claude.com/docs/en/skills, fetched 2026-07-16):
a skill is a directory whose SKILL.md holds YAML frontmatter (all fields
optional) plus a markdown body. The DESCRIPTION is always in context so the
model knows what exists; the BODY loads only when the skill is invoked — by
the user typing /<name>, or by the model calling the `skill` tool. That
progressive disclosure is the point: long procedures cost ~nothing until used.

minicc mirrors the contract under its own config root (the hooks precedent —
CC's schema, .minicc/ paths), so a CC-authored SKILL.md drops in unchanged:

    ~/.minicc/skills/<name>/SKILL.md                    personal (all projects)
    <cwd & ancestors>/.minicc/skills/<name>/SKILL.md    project

Personal overrides project on a name clash (CC: enterprise > personal >
project; minicc has no enterprise layer). Within the project level the
directory closest to cwd wins. There is no legacy commands/ dir to honor —
minicc never had one.

Honored frontmatter: name (display only — the command word is the directory
name, per CC), description, when_to_use, argument-hint, arguments,
disable-model-invocation, user-invocable, allowed-tools. Malformed YAML loads
the body with EMPTY metadata (CC-documented behavior — /name still works).
Unknown fields are tolerated silently.

Discovery rescans the filesystem on every lookup — CC's "live change
detection" achieved by rescan instead of file watchers (the scan is a handful
of globs). The always-in-context LISTING is delivered CC's way too: as a
<system-reminder> in the message stream, re-injected when skills change
(reminders.py + watch.py polling at prompt boundaries) — never in the cached
tools+system prefix, so updates cost one appended block, not a cache rebuild.

Deliberate cuts (absent infra; the full ledger lives in SKILL_DESIGN.md): plugins /
enterprise / bundled skills, nested directory-qualified names, context:fork,
per-skill hooks/model/effort/paths, disallowed-tools, skillOverrides.
"""

import hashlib
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from minicc import config, permissions

_LISTING_DESC_CAP = 1536  # CC: combined description + when_to_use cap per entry
_SHELL_TIMEOUT = 60  # seconds per !`cmd` / ```! block (CC doesn't document one)

_SESSION_ID: str | None = None  # feeds ${CLAUDE_SESSION_ID}
_LOADED: dict[str, str] = {}  # skill name → hash of last rendered content (dedup)


def reset(session_id: str | None = None) -> None:
    """New session / /clear: set the session id and forget what's loaded."""
    global _SESSION_ID
    _SESSION_ID = session_id
    _LOADED.clear()


# ─── SKILL.md parsing ─────────────────────────────────────────────────────────
# Hand-rolled shallow YAML subset (no pyyaml dependency): `key: value` scalars,
# inline [a, b] lists, block "- item" lists, quotes, booleans. Skill frontmatter
# is flat by design, and CC's documented failure mode — malformed YAML → body
# with empty metadata — makes a forgiving parser the FAITHFUL choice.


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _parse_value(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_unquote(x.strip()) for x in v[1:-1].split(",") if x.strip()]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return _unquote(v)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """(meta, body). No frontmatter → ({}, whole text). A fenced block that
    fails to parse → ({}, body) — CC loads the body with empty metadata."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), None
    )
    if end is None:  # unterminated fence: treat the whole file as body
        return {}, text
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    meta: dict = {}
    key = None
    try:
        for raw in lines[1:end]:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") and key is not None:
                cur = meta.get(key)
                if not isinstance(cur, list):
                    meta[key] = [] if cur in ("", None) else [cur]
                meta[key].append(_parse_value(stripped[2:]))
                continue
            if ":" not in stripped:
                raise ValueError(f"not key: value — {raw!r}")
            k, _, v = stripped.partition(":")
            if not k.strip():
                raise ValueError(f"empty key — {raw!r}")
            key = k.strip().lower()
            meta[key] = _parse_value(v) if v.strip() else ""
    except Exception:
        return {}, body
    return meta, body


def _as_list(v) -> list[str]:
    """Normalize a frontmatter value: YAML list, or space/comma-separated string."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x for x in re.split(r"[,\s]+", str(v or "")) if x]


def _split_tool_entries(v) -> list[str]:
    """allowed-tools entries: space/comma-separated, but `bash(git add *)`
    contains spaces — split only OUTSIDE parentheses."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in str(v or ""):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in " ,\t\n" and depth == 0:
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


# ─── discovery ────────────────────────────────────────────────────────────────


@dataclass
class Skill:
    name: str  # the command word = directory name (frontmatter `name` is display-only)
    dir: Path
    meta: dict = field(default_factory=dict)
    body: str = ""

    @property
    def description(self) -> str:
        """Listing text: description (falling back to the body's first
        paragraph, per CC) + when_to_use, capped at 1,536 chars (CC's cap)."""
        d = str(self.meta.get("description") or "").strip()
        if not d:
            d = self.body.strip().split("\n\n")[0].replace("\n", " ").strip()
        w = str(self.meta.get("when_to_use") or "").strip()
        return f"{d} {w}".strip()[:_LISTING_DESC_CAP]

    def flag(self, key: str) -> bool:
        return self.meta.get(key) is True

    @property
    def hint(self) -> str:
        """argument-hint for listings. CC's docs write `argument-hint:
        [issue-number]` — YAML reads that as a flow list, so re-bracket lists."""
        h = self.meta.get("argument-hint")
        if isinstance(h, list):
            return " ".join(f"[{x}]" for x in h)
        return str(h or "")


def discover() -> dict[str, Skill]:
    """Rescan all skill roots. Later roots override earlier on a name clash."""
    found: dict[str, Skill] = {}
    for root in config.config_roots("skills"):
        if not root.is_dir():
            continue
        for md in sorted(root.glob("*/SKILL.md")):
            try:
                meta, body = parse_frontmatter(md.read_text())
            except OSError:
                continue
            found[md.parent.name] = Skill(md.parent.name, md.parent, meta, body)
    return found


def lookup(name: str) -> Skill | None:
    return discover().get(name)


def skill_md_paths():
    """Every SKILL.md currently on disk — the watch-snapshot surface for the
    skills reminder (adds/removes/edits all change the mtime dict)."""
    for root in config.config_roots("skills"):
        if root.is_dir():
            yield from sorted(root.glob("*/SKILL.md"))


def listing_text() -> str:
    """The always-in-context listing. Injected as a <system-reminder> in the
    message stream (reminders.py — CC's placement; hot updates without busting
    the prefix cache). disable-model-invocation skills are OMITTED — CC keeps
    their description out of context entirely; user-invocable:false skills
    stay listed. "" when no skills exist (no reminder at all)."""
    items = [
        sk
        for _n, sk in sorted(discover().items())
        if not sk.flag("disable-model-invocation")
    ]
    if not items:
        return ""
    # Entry shape "- name: description" matches observed CC listings.
    # argument-hint is NOT shown here: CC scopes it to / autocomplete (the
    # doc's words), so minicc's equivalent surface is /help, not the model.
    return "\n".join(f"- {sk.name}: {sk.description}" for sk in items)


# ─── rendering: substitutions, then shell preprocessing ──────────────────────
# Order matters and is pinned by the docs: ${CLAUDE_SKILL_DIR} is documented for
# use INSIDE !`cmd` lines, so variables resolve before commands run; command
# output is inserted as plain text and never re-scanned (single pass).

_TOKEN = re.compile(
    r"(?P<bs>\\+)?\$(?:"
    r"\{(?P<braced>[A-Z_]+)\}"
    r"|ARGUMENTS\[(?P<idx>\d+)\]"
    r"|(?P<all>ARGUMENTS)"
    r"|(?P<num>\d+)"
    r"|(?P<named>[A-Za-z_][A-Za-z0-9_]*)"
    r")"
)

# !`cmd` counts only at line start or after whitespace (KEY=!`cmd` stays
# literal); ```! fenced blocks run as one multi-line script. One combined
# pattern = one pass, so output can't smuggle in a placeholder for a later pass.
_SHELL = re.compile(
    r"^```![ \t]*\n(?P<fenced>.*?)\n```[ \t]*$" r"|(?:(?<=\s)|^)!`(?P<inline>[^`\n]+)`",
    re.M | re.S,
)


def _substitute(body: str, sk: Skill, raw_args: str) -> tuple[str, bool]:
    """Apply CC's string substitutions. Returns (text, any-argument-token-used).

    Escapes per the docs: exactly one backslash before a token makes it literal
    (backslash consumed); two or more leave every backslash AND expand. $words
    that aren't declared argument names stay literal (a $PATH in a bash snippet
    must survive)."""
    try:
        argv = shlex.split(raw_args)  # indexed args use shell-style quoting
    except ValueError:
        argv = raw_args.split()
    declared = _as_list(sk.meta.get("arguments"))
    braced_vals = {
        "CLAUDE_SESSION_ID": _SESSION_ID or "",
        "CLAUDE_SKILL_DIR": str(sk.dir),
        "CLAUDE_PROJECT_DIR": str(Path.cwd()),
    }
    used = False

    def repl(m: re.Match) -> str:
        nonlocal used
        bs = m.group("bs") or ""
        if len(bs) == 1:
            return m.group(0)[1:]  # escaped: drop the backslash, keep the token
        if m.group("braced"):
            v = braced_vals.get(m.group("braced"))
            return m.group(0) if v is None else bs + v  # unknown ${…} stays literal
        if m.group("named"):
            if m.group("named") not in declared:
                return m.group(0)
            used = True
            i = declared.index(m.group("named"))
            return bs + (argv[i] if i < len(argv) else "")
        used = True
        if m.group("all") is not None:
            return bs + raw_args  # $ARGUMENTS = the full string as typed
        i = int(m.group("idx") if m.group("idx") is not None else m.group("num"))
        return bs + (argv[i] if i < len(argv) else "")

    return _TOKEN.sub(repl, body), used


def _run_shell(cmd: str) -> str:
    if config.skill_shell_disabled():
        return "[shell command execution disabled by policy]"  # CC's replacement text
    try:
        out = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT,
            cwd=Path.cwd(),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"[command failed: {e}]"
    parts = [p for p in (out.stdout.strip(), out.stderr.strip()) if p]
    return "\n".join(parts)


def render(sk: Skill, args: str = "") -> str:
    body, used_args = _substitute(sk.body, sk, args or "")
    body = _SHELL.sub(
        lambda m: _run_shell(m.group("fenced") or m.group("inline")), body
    )
    if (args or "").strip() and not used_args:
        # CC: args with no placeholder to receive them are appended verbatim
        body += f"\n\nARGUMENTS: {args}"
    return body


def render_tracked(sk: Skill, args: str = "") -> tuple[str, bool]:
    """(rendered content, already-loaded). CC v2.1.202+: re-invoking a skill
    whose rendered content is identical to the copy in context gets a short
    note instead of a second full copy."""
    content = render(sk, args)
    h = hashlib.sha256(content.encode()).hexdigest()
    dup = _LOADED.get(sk.name) == h
    _LOADED[sk.name] = h
    return content, dup


def apply_grants(sk: Skill) -> list[str]:
    """allowed-tools grants, active until the next user prompt. ${CLAUDE_*_DIR}
    resolves inside rules too (CC parity: a Bash(${CLAUDE_PROJECT_DIR}/x *) rule
    matches the same path the body uses)."""
    entries = [
        e.replace("${CLAUDE_SKILL_DIR}", str(sk.dir)).replace(
            "${CLAUDE_PROJECT_DIR}", str(Path.cwd())
        )
        for e in _split_tool_entries(sk.meta.get("allowed-tools"))
    ]
    if entries:
        permissions.grant_skill_tools(entries)
    return entries


def expand(sk: Skill, args: str = "") -> str:
    """The rendered skill body behind its "Base directory for this skill:"
    header — CC's expansion shape on BOTH paths (user-path and Skill-tool
    tool_result probed live, 2026-07-16/17)."""
    content, _dup = render_tracked(sk, args)
    apply_grants(sk)
    return f"Base directory for this skill: {sk.dir}\n\n{content}"


def user_invoke(name: str, args: str = "") -> tuple[str, str] | None:
    """/<name> from the REPL. None = no such typable skill (unknown name, or
    user-invocable: false). Returns CC's two-part slash expansion (probed live;
    same shape in B's /init transcript): the command-tags message
    (<command-message>/<command-name>, plus <command-args> only when args were
    given) and the expansion message (base-dir header + rendered body). The
    cli appends them as two user messages; the transcript marks the second
    one `meta` (CC's isMeta)."""
    sk = lookup(name)
    if sk is None or sk.meta.get("user-invocable") is False:
        return None
    tags = [
        f"<command-message>{name}</command-message>",
        f"<command-name>/{name}</command-name>",
    ]
    if args:
        tags.append(f"<command-args>{args}</command-args>")
    return "\n".join(tags), expand(sk, args)
