"""System-reminder injection — CC's delivery mechanism for volatile context.

CC does NOT put CLAUDE.md, the auto-memory index, the date, or the skill
listing in the cached tools+system prefix. It injects them as
<system-reminder> blocks in the MESSAGE STREAM, attached to user turns.
Established three ways (2026-07-16): observed live in a CC Fable 5 session
(the claudeMd block and the skills listing arrive as system-reminders on user
turns — wording below is copied from that observation); CC's own tool
descriptions say so ("Available skills appear in a system-reminder listing");
and the archived B/C experiment transcripts contain no reminder text at all —
CC re-derives reminders at request-assembly time instead of persisting them.

Why: prompt caching is prefix caching. Volatile content in the prefix busts
the whole layer every time it changes; APPENDED messages invalidate nothing.
Reminders put hot-updatable context on the append side, so a mid-session
CLAUDE.md edit or a new skill costs one re-injected block, not a cache
rebuild. (minicc's previous design — CLAUDE.md/memory as a dedicated prefix
layer, skills in the tool description — matched CC's LEGACY layout; the
SLASH_COMMAND_TOOL_CHAR_BUDGET env-var name is the fossil of it.)

The injection rule, evaluated at every user prompt (cli calls for_prompt),
is PER KIND — each mirrors its documented CC behavior (memory + skills docs,
re-fetched 2026-07-16):

- claudeMd (CLAUDE.md + memory index + date): injected at session start, and
  re-injected — re-read fresh from disk — only when the copy is LOST from the
  live history (post-compact: "Claude re-reads it from disk and re-injects
  it"; also /clear, resume, rewind, rollback — the history scan makes this
  self-healing) or on invalidate(). Mid-session edits are deliberately NOT
  watched: CC loads these "at the start of every conversation" and mid-session
  CLAUDE.md edits don't apply until restart//clear. (v1 of this module watched
  mtimes and hot-reloaded — MORE live than CC; reverted for 1:1.)
- skills listing: additionally mtime-watched (watch.Poller over every
  SKILL.md) — skills DO have documented live change detection, so add/remove/
  description edits re-inject an updated listing at the next prompt.

Reminders ride INSIDE the same user message as the typed query, reminder text
first (the observed shape of CC's own turns), and are NOT persisted to the
session transcript (CC parity — its JSONLs carry none; the transcript stores
the bare query).
"""

from datetime import date
from pathlib import Path

from minicc import agents, config, memory, skills, watch

# In-context markers: distinctive first lines of each reminder kind, used to
# detect whether a live copy is still in the history (vs compacted/rewound away).
_CLAUDE_MARKER = "As you answer the user's questions, you can use the following context:"
_SKILLS_MARKER = "The following skills are available for use with the Skill tool:"
_AGENTS_MARKER = "Available agent types for the Agent tool:"

_last: dict = {}  # kind → last injected text
_pollers: dict = {}  # kind → watch.Poller


def reset():
    """New session / /clear: forget injections and re-seed pollers."""
    _last.clear()
    _pollers.clear()


def invalidate():
    """Force a rebuild at the next prompt even though no watched file changed —
    for state flips with no file signature (e.g. /memory on|off)."""
    _pollers.clear()


def _claude_md_snapshot() -> str:
    """Constant: the claudeMd sources are NOT watched (CC parity — see module
    docstring). The poller fires once per reset()/invalidate(); afterwards only
    loss-from-history triggers a rebuild, which re-reads disk at that moment."""
    return "static"


def _skills_snapshot() -> dict:
    return watch.mtime_snapshot(skills.skill_md_paths())


def _read_claude_md(path: Path) -> str:
    """One CLAUDE.md, in full. "" if missing/empty.

    No truncation: CC's memory doc is explicit — "CLAUDE.md files are loaded
    in full regardless of length"; the 200-line/25KB limit "applies only to
    MEMORY.md" (which minicc enforces in memory.load_index). This function
    truncated until 2026-07-16, citing "CC's limits" — a misattribution of the
    MEMORY.md-only limit, caught by re-reading the official page."""
    if not path.exists():
        return ""
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def claude_md_files() -> list[tuple[Path, str]]:
    """(path, text) for CLAUDE.md in cwd AND every ancestor directory.

    CC's monorepo behavior: parent directories' CLAUDE.md files are pulled in
    automatically alongside the project's own (root conventions + subproject
    specifics), outermost first so the nearest file reads last and wins where
    they disagree. Each file loads IN FULL (see _read_claude_md).

    Lives here (not prompts/) because CLAUDE.md is delivered by the claudeMd
    system-reminder below — CC's message-stream mechanism, NOT a system-prompt
    cache layer (the old load_project_context design; see the module docstring
    for why it moved).
    Recorded divergences (memory doc, re-read 2026-07-16): no ~/.claude global
    CLAUDE.md (auto-memory covers that role), no CLAUDE.local.md, no on-demand
    child-directory loading, no ./.claude/CLAUDE.md alternate location, no
    @path imports (CC expands them at launch, depth 4), no stripping of
    block-level HTML comments before injection.
    """
    view = config.current_settings()
    if not view.trusted:
        return []
    cwd = view.snapshot.start_dir
    out = []
    for d in [*reversed(cwd.parents), cwd]:  # outermost → cwd
        text = _read_claude_md(d / "CLAUDE.md")
        if text:
            out.append((d / "CLAUDE.md", text))
    return out


def _claude_md_text() -> str:
    """The claudeMd reminder, byte-matched to CC's observed format (a live
    session's opening reminder + a CLI probe with a project CLAUDE.md,
    2026-07-16): sections separated by SINGLE newlines — blank lines appear
    only after each "Contents of <path> (<label>):" header and before the
    trailing disclaimer. Labels verbatim from those observations. CC also
    emits a # userEmail section there; minicc has no account, so it's skipped
    (recorded divergence)."""
    sections = []
    mem = memory.load_index()
    if mem:
        sections.append(
            f"Contents of {memory.store_dir() / memory.INDEX_NAME} "
            f"(user's auto-memory, persists across conversations):\n\n{mem}"
        )
    for path, text in claude_md_files():
        sections.append(
            f"Contents of {path} (project instructions, checked into the "
            f"codebase):\n\n{text}"
        )
    parts = [_CLAUDE_MARKER]
    if sections:
        parts.append(
            "# claudeMd\n"
            "Codebase and user instructions are shown below. Be sure to adhere "
            "to these instructions. IMPORTANT: These instructions OVERRIDE any "
            "default behavior and you MUST follow them exactly as written.\n\n"
            + "\n\n".join(sections)
        )
    parts.append(f"# currentDate\nToday's date is {date.today().isoformat()}.")
    body = "\n".join(parts)
    disclaimer = (
        "      IMPORTANT: this context may or may not be relevant to your "
        "tasks. You should not respond to this context unless it is highly "
        "relevant to your task."
    )
    return f"<system-reminder>\n{body}\n\n{disclaimer}\n</system-reminder>"


def _skills_text() -> str:
    listing = skills.listing_text()
    if not listing:
        return ""
    return f"<system-reminder>\n{_SKILLS_MARKER}\n\n{listing}\n</system-reminder>"


def _agents_snapshot() -> dict:
    # built-in types are constant; only agent definition files change (mtime).
    return watch.mtime_snapshot(agents.agent_md_paths())


def _agents_text() -> str:
    # always non-empty — the built-in types (general-purpose/explore) always
    # exist, so this reminder rides every session the way CC's does.
    return f"<system-reminder>\n{_AGENTS_MARKER}\n\n{agents.listing_text()}\n</system-reminder>"


_KINDS = [
    ("claude_md", _CLAUDE_MARKER, _claude_md_snapshot, _claude_md_text),
    ("skills", _SKILLS_MARKER, _skills_snapshot, _skills_text),
    ("agents", _AGENTS_MARKER, _agents_snapshot, _agents_text),
]


def for_prompt(history) -> list[str]:
    """Reminder texts to inject before this user message (possibly empty).

    Per kind: skip when nothing changed AND a copy is still in the live
    history. Otherwise rebuild; inject unless the history copy is already
    byte-identical (a touch with no content change). A copy lost to
    compaction, /rewind, or an interrupted-turn rollback re-injects here
    without any bookkeeping — the history scan is the source of truth."""
    hay = [
        m.get("content", "")
        for m in history
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    out = []
    for kind, marker, snapshot_fn, build in _KINDS:
        poller = _pollers.get(kind)
        if poller is None:
            poller = _pollers[kind] = watch.Poller(snapshot_fn)
        dirty = poller.changed()  # first call after reset() is always True
        in_ctx = any(marker in h for h in hay)
        if not dirty and in_ctx:
            continue
        text = build()
        if not text:
            _last.pop(kind, None)  # e.g. all skills removed: nothing to say
            continue
        if in_ctx and text == _last.get(kind):
            continue
        _last[kind] = text
        out.append(text)
    return out
