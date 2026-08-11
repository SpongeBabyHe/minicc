import argparse
import os
import platform
import readline  # noqa: F401 — importing enables history + line editing for input()
import re
import select
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from rich.text import Text
from minicc import context_management, llm
from minicc.query_engine import agent_loop
from minicc import ux
from minicc.llm import get_usage
from minicc import permissions
from minicc import sessions
from minicc import config
from minicc import checkpoints
from minicc import memory
from minicc import hooks
from minicc import reminders
from minicc import skills
from minicc import trust
from minicc import tools as tool_registry
from minicc.tools import freshness
from minicc.prompts.system import build_session_context
from minicc.workspace import local_settings_are_repository_supplied


# Sonnet 4.6 pricing (USD per 1M tokens). Update if you switch models.
_PRICE_INPUT_PER_M = 3.0
_PRICE_OUTPUT_PER_M = 15.0
_PRICE_CACHE_WRITE_PER_M = 3.75  # 1.25x input
_PRICE_CACHE_READ_PER_M = 0.30  # 0.1x input

# /init: a canned instruction run as a normal agent turn — the model explores with
# its own tools and writes CLAUDE.md. No special machinery; just a good prompt.
#
# The skeleton is CC's official /init prompt, recovered VERBATIM from a CC session
# transcript during the three-arm /init experiment (2026-07-14) — its proven parts
# are kept word-for-word (single-test requirement, the "requires reading multiple
# files" inclusion bar, the banned-genericism examples, rules-file checks, the
# anti-fabrication clause, the standard header for cross-tool interop). Three
# additions the experiment showed the original lacks:
#   1. verify-before-write — no arm's unverified command survived checking; the
#      wording is Fable 5's own spontaneous phrasing from that run.
#   2. universal-quantifier check — "all"/"only" claims were the other error class.
#   3. batch parallel reads — turn count drove a ~4x context-traffic spread.
# Deliberately absent (the experiment showed CC needs neither): todo/task
# scaffolding hints.
_INIT_PROMPT = (
    "Please analyze this codebase and create a CLAUDE.md file, which will be given "
    "to future AI coding agents (Claude Code, minicc) operating in this repository.\n\n"
    "What to add:\n"
    "1. Commands that will be commonly used, such as how to build, lint, and run "
    "tests. Include the necessary commands to develop in this codebase, such as "
    "how to run a single test.\n"
    "2. High-level code architecture and structure so that future instances can be "
    'productive more quickly. Focus on the "big picture" architecture that '
    "requires reading multiple files to understand.\n\n"
    "Usage notes:\n"
    "- VERIFY each command works by actually running it (bash) before documenting "
    "it; if it needs env vars or a .env file, say so next to the command. If you "
    "cannot verify a command, do not present it as working.\n"
    '- Before writing a claim containing "all", "only", "both" or '
    '"never", check each member it quantifies over.\n'
    "- Explore with glob/grep/read_file; batch independent file reads as parallel "
    "tool calls in a single turn — each extra turn re-reads the whole context.\n"
    "- If there's already a CLAUDE.md, improve it in place rather than duplicating.\n"
    "- Do not repeat yourself and do not include obvious instructions like "
    '"Provide helpful error messages to users", "Write unit tests for all new '
    'utilities", "Never include sensitive information (API keys, tokens) in '
    'code or commits".\n'
    "- Avoid listing every component or file structure that can be easily "
    "discovered.\n"
    "- Don't include generic development practices.\n"
    "- If there are Cursor rules (in .cursor/rules/ or .cursorrules) or Copilot "
    "rules (in .github/copilot-instructions.md), make sure to include the "
    "important parts.\n"
    "- If there is a README.md, make sure to include the important parts.\n"
    '- Do not make up information such as "Common Development Tasks", "Tips '
    'for Development", "Support and Documentation" unless this is expressly '
    "included in other files that you read.\n"
    "- Be sure to prefix the file with the following text:\n\n"
    "```\n"
    "# CLAUDE.md\n\n"
    "This file provides guidance to Claude Code (claude.ai/code) when working with "
    "code in this repository.\n"
    "```\n\n"
    "Write the result with write_file."
)


def _fire_session_start(session_id: str, source: str) -> str:
    """Run SessionStart hooks (source: startup, resume, clear, or compact).

    context_management.manager fires the compact source after an in-place
    compaction; the other sources enter here.
    Returns hook-injected context to append to the session-context layer
    ("" if none) — CC injects additionalContext into context at session start the
    same way."""
    d = hooks.run(
        "SessionStart",
        session_id=session_id,
        match_value=source,
        source=source,
        model=llm.get_model(),
    )
    hooks.surface(d)
    hooks.raise_if_stopped(d, "SessionStart")
    return "\n".join(d.additional_context)


def _session_context_with_hooks(session_id: str, source: str) -> str:
    """The session-context layer (env + git) plus any SessionStart hook context."""
    ctx = build_session_context(include_git=config.current_settings().trusted)
    extra = _fire_session_start(session_id, source)
    if extra:
        ctx += f"\n\n# Context from SessionStart hook\n\n{extra}"
    return ctx


def _fire_session_end(session_id: str, reason: str) -> None:
    """Run SessionEnd hooks (reason: "clear" | "prompt_input_exit" — CC's other
    reasons target absent infra). Event-specific block decisions are ignored;
    universal continue:false still stops processing."""
    d = hooks.run(
        "SessionEnd", session_id=session_id, match_value=reason, reason=reason
    )
    hooks.surface(d)
    hooks.raise_if_stopped(d, "SessionEnd")


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        return out or "untracked"
    except Exception:
        return "no-git"


def _session_info() -> dict:
    """Pure data about this session — no presentation."""
    info = {
        "SESSION": datetime.now().isoformat(timespec="seconds"),
        "commit": _git_sha() if config.current_settings().trusted else "restricted",
        "model": llm.get_model(),
        "cwd": str(Path.cwd()),
        "os": platform.system(),
    }
    if (Path.cwd() / "CLAUDE.md").exists():
        info["CLAUDE.md"] = (
            "found (injected as a system-reminder at first prompt)"
            if config.current_settings().trusted
            else "found (disabled in restricted mode)"
        )
    return info


def _cmd_help():
    ux.say(
        ux.kv_block(
            [
                ("/help", "Show this help"),
                ("/clear", "Reset conversation history and tool permissions"),
                ("/init", "Scan the project and write/refresh CLAUDE.md"),
                ("/context", "Show context token usage vs the compaction budget"),
                ("/cost", "Show token usage and estimated cost"),
                (
                    "/model [default] [id]",
                    "Show / switch session / set persistent default model",
                ),
                ("/compact [focus]",
                 "Summarize older history now (optional focus)"),
                ("/recap", "Show a summary without changing history"),
                (
                    "/memory [file|on|off|consolidate]",
                    "Browse, toggle, or tidy cross-session memory",
                ),
                (
                    "/rewind [N] [mode]",
                    "List restore points; restore code (default) | conversation | both",
                ),
                ("q / exit / quit", "Leave minicc"),
            ]
            + [  # user-invocable skills (SKILL_DESIGN.md); rescan = live view
                (
                    f"/{n}" + (f" {sk.hint}" if sk.hint else ""),
                    f"skill: {sk.description[:100]}",
                )
                for n, sk in sorted(skills.discover().items())
                if sk.meta.get("user-invocable") is not False
            ]
        )
    )


def _cmd_cost():
    u = get_usage()
    cost = (
        u["input"] * _PRICE_INPUT_PER_M
        + u["output"] * _PRICE_OUTPUT_PER_M
        + u["cache_read"] * _PRICE_CACHE_READ_PER_M
        + u["cache_creation"] * _PRICE_CACHE_WRITE_PER_M
    ) / 1_000_000
    cost += u.get("web_searches", 0) * 0.01  # server web_search: $10 per 1,000
    total_in = u["input"] + u["cache_read"] + u["cache_creation"]
    hit = (u["cache_read"] / total_in * 100) if total_in else 0
    ux.say(
        ux.kv_block(
            [
                ("uncached input", f"{u['input']:,}"),
                ("cache read", f"{u['cache_read']:,}  ({hit:.0f}% hit rate)"),
                ("cache write", f"{u['cache_creation']:,}"),
                ("output", f"{u['output']:,}"),
                ("web searches", f"{u.get('web_searches', 0):,}"),
                ("est. cost", f"${cost:.4f}"),
            ]
        )
    )


def _cmd_context(messages, ctx):
    """
    Show context token usage vs the compaction budget.
    """
    c = context_management.context_usage(
        ctx,
        messages,
        model=llm.get_model(),
    )
    ux.say(
        ux.kv_block(
            [
                (
                    "context tokens",
                    f"{c['estimated_tokens']:,}  (~{c['pct_of_budget']:.0f}% of compaction budget)",
                ),
                (
                    "compaction budget",
                    f"{c['budget']:,}  (auto-compaction triggers above this)",
                ),
                ("messages", str(c["messages"])),
                ("tool_results",
                 f"{c['tool_results']} total, {c['evicted']} cleared in working set"),
                ("eviction events", str(c["eviction_events"])),
                ("results cleared", str(c["evicted_tool_results"])),
                ("tokens reclaimed", f"~{c['evicted_tokens']:,}"),
                (
                    "last invalidated suffix",
                    f"~{c['last_eviction_suffix_tokens']:,} tokens",
                ),
                ("compaction events", str(c["compaction_events"])),
            ]
        )
    )
    ux.say(
        "(last API total + estimated full-request delta; cold reads include system/tools)",
        style=ux.S_INFO,
    )


def _cmd_compact(
    messages, ctx, focus: str | None = None, session_id: str | None = None
):
    """Manually compact history. Mutates `messages` in place."""
    did = context_management.compact(
        ctx,
        messages,
        focus=focus,
        session_id=session_id,
        trigger="manual",
        runtime=llm.summary_runtime(),
    )
    if did:
        ux.say("conversation history compacted", style=ux.S_INFO)
    elif did is False:
        ux.say("nothing to compact yet", style=ux.S_INFO)


def _cmd_recap(messages):
    """Show a summary of the conversation without changing it."""
    summary = context_management.recap(
        messages,
        runtime=llm.summary_runtime(),
    )
    ux.say("<<< RECAP (history unchanged)", style=ux.S_ASSISTANT)
    ux.markdown(summary)


def _cmd_memory(arg: str | None):
    """Browse, toggle, or consolidate auto-memory. `/memory` lists the store;
    `/memory <file>` views one file; `/memory on|off` toggles it for this session;
    `/memory consolidate` runs an Auto-Dream-style tidy pass (merge duplicates,
    retire stale facts, fix dates, tighten the index)."""
    if arg in ("on", "off"):
        memory.set_enabled(arg == "on")
        # no file changed, so the reminder poller wouldn't notice — force it
        reminders.invalidate()
        ux.say(
            f"auto-memory {'enabled' if arg == 'on' else 'disabled'}", style=ux.S_INFO
        )
        return
    if arg == "consolidate":
        if not memory.enabled():
            ux.say("auto-memory is off (/memory on first)", style=ux.S_ERROR)
            return
        ux.say(
            "[consolidating memory — writes will ask for approval; answer 'all' to approve the batch]",
            style=ux.S_INFO,
        )
        summary = memory.consolidate()
        # the tidied MEMORY.md re-injects via the reminder poller at next prompt
        ux.markdown(summary)
        return
    if arg:
        path = arg if arg.startswith("/memories") else f"/memories/{arg}"
        ux.say(memory.view(path))
        return
    ux.say(
        ux.kv_block(
            [
                ("auto-memory", "on" if memory.enabled() else "off"),
                ("store", str(memory.store_dir())),
            ]
        )
    )
    ux.say(memory.view("/memories"))


# Short aliases for ergonomics; /model also accepts any raw model id.
_MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "fable": "claude-fable-5",
}


def _cmd_model(arg: str | None):
    """Show the model, switch it for this session, or set the persistent default.

    /model                 → show current (session) + default (persisted) + aliases
    /model <alias|id>      → switch for this session only (reverts on restart)
    /model default <a|id>  → set the persistent user default + switch
    """
    if not arg:
        cur = llm.get_model()
        rows = [
            ("current (session)", cur),
            ("default (persisted)", config.resolve_model()),
        ]
        rows += [(alias, mid) for alias, mid in _MODEL_ALIASES.items()]
        ux.say(ux.kv_block(rows))
        ux.say(
            "usage: /model <alias|id>  ·  /model default <alias|id>", style=ux.S_INFO
        )
        return

    parts = arg.split(maxsplit=1)
    if parts[0] == "default":
        if len(parts) < 2:
            ux.say("usage: /model default <alias|id>", style=ux.S_ERROR)
            return
        target = _MODEL_ALIASES.get(parts[1].strip(), parts[1].strip())
        # Always user-scoped for now. Project scopes have no command surface yet.
        # resolve_model already reads project > user, but there's no command
        # surface (e.g. --project) to write a per-project default yet — deferred.
        config.set_default_model(target)
        llm.set_model(target)
        ux.say(
            f"default model → {target}  (persisted for this user + switched)",
            style=ux.S_INFO,
        )
        return

    target = _MODEL_ALIASES.get(arg.strip(), arg.strip())
    llm.set_model(target)
    ux.say(f"model → {target}  (this session)", style=ux.S_INFO)


def _cmd_rewind(history, arg: str | None, session_id: str | None = None, ctx=None):
    """List restore points, or `/rewind N [code|conversation|both]` to restore.
    N is the position in the /rewind list (every turn is a restore point, like
    CC's one-checkpoint-per-prompt). Modes: `code` (default) reverts files and
    keeps the conversation; `conversation` replays the transcript back to just
    before turn N's prompt (files kept); `both` does both."""
    points = checkpoints.restore_points()  # [(turn, query, changed_paths)]
    if arg is None:
        if not points:
            ux.say("nothing to rewind yet", style=ux.S_INFO)
            return
        rows = []
        for i, (_, q, paths) in enumerate(points, 1):
            mark = f"  [{len(paths)} file(s)]" if paths else ""
            rows.append((f"[{i}]", ux.truncate(q, 60) + mark))
        ux.say(ux.kv_block(rows))
        ux.say(
            "usage: /rewind <n> [code|conversation|both] — code (default) reverts "
            "files, conversation replays history back to before that prompt; "
            "bash-made changes aren't tracked.",
            style=ux.S_INFO,
        )
        return
    parts = arg.split()
    mode = parts[1] if len(parts) > 1 else "code"
    try:
        n = int(parts[0])
    except ValueError:
        ux.say("usage: /rewind <n> [code|conversation|both]", style=ux.S_ERROR)
        return
    if mode not in ("code", "conversation", "both"):
        ux.say(
            f"unknown mode {mode!r} (code | conversation | both)", style=ux.S_ERROR)
        return
    if not (1 <= n <= len(points)):
        ux.say(
            f"no restore point [{n}]  (try /rewind to list)", style=ux.S_ERROR)
        return
    turn, query, _ = points[n - 1]
    # read the conversation anchor BEFORE restore_files trims the stack
    events = checkpoints.events_at(turn)

    if mode in ("code", "both"):
        restored, failed = checkpoints.restore_files(turn)
        msg = f"reverted {restored} file change(s) to restore point {n}"
        if failed:
            msg += (
                f"  — {len(failed)} could not be restored; "
                f"checkpoint retained for retry: {', '.join(failed)}"
            )
        ux.say(msg, style=ux.S_INFO)

    if mode in ("conversation", "both"):
        if session_id is None or events is None:
            ux.say("no transcript to rewind from", style=ux.S_ERROR)
            return
        try:
            rewound = sessions.load_upto(session_id, events)
        except sessions.SessionError as error:
            ux.say(f"could not rewind conversation: {error}", style=ux.S_ERROR)
            return
        history[:] = rewound
        sessions.log_rewind(session_id, history)  # append-only: a reset event
        if ctx:
            ctx.reset_size()  # forget the pre-rewind size (avoid spurious compact)
        ux.say(
            f"conversation rewound to before turn {turn}; its prompt was: {ux.truncate(query, 80)}",
            style=ux.S_INFO,
        )
    elif mode == "code":
        # code-only: the conversation continues, so tell the model files changed
        notice = {
            "role": "user",
            "content": "[Files were rewound to an earlier checkpoint; edits made since then are undone.]",
        }
        history.append(notice)
        if session_id:
            sessions.append_message(session_id, notice)


def _cmd_clear(history, session_id: str) -> str:
    """Rotate to a fresh session: end the old one, reset per-session state, and
    return the new session id (the pre-clear transcript stays on disk; new turns
    record to a fresh `<id>.jsonl`). The caller resets its turn counter."""
    _fire_session_end(
        session_id, "clear")  # old session ends (CC reason "clear")
    history.clear()
    new_id = sessions.new_id()
    config.refresh_active_settings()
    tool_registry.configure_from_settings()
    permissions.reset()
    permissions.preload(config.allowed_tools())  # keep settings-trusted tools
    checkpoints.activate(new_id)
    hooks.reset()  # re-read hook config (settings may have changed)
    freshness.reset()  # new session: read-before-edit starts over
    skills.reset(new_id)  # new session id; forget loaded skills
    reminders.reset()  # fresh claudeMd/skills reminders on next prompt
    # refresh env/git + SessionStart(source="clear") hook context
    llm.set_session_context(_session_context_with_hooks(new_id, "clear"))
    ux.say("conversation, permissions reset", style=ux.S_INFO)
    return new_id


def _print_startup_banner(
    pre_approved,
    refused,
    session_id,
    history,
    resumed: bool = False,
) -> None:
    """The framed session header: identity, persisted trust that skips prompts,
    available skills, and a resume note."""
    ux.console.rule()
    ux.say(ux.kv_block(list(_session_info().items()), indent=""), style=ux.S_INFO)
    if pre_approved:
        ux.say(
            f"pre-approved (no prompt) from settings: {', '.join(sorted(pre_approved))}",
            style=ux.S_INFO,
        )
    if bash_rules := config.permission_allow_rules():
        ux.say(  # persistent trust stays visible (PERMISSIONS.md principle)
            f"bash allow rules from settings: {', '.join(bash_rules)}", style=ux.S_INFO
        )
    if skill_names := sorted(skills.discover()):
        ux.say(
            f"skills: {', '.join('/' + n for n in skill_names)}", style=ux.S_INFO)
    if refused:
        ux.say(
            f"settings list {', '.join(refused)} but it can't be pre-approved "
            "(approve per session — see PERMISSIONS.md)",
            style=ux.S_INFO,
        )
    if resumed:
        ux.say(
            f"resumed session {session_id} ({len(history)} messages)", style=ux.S_INFO
        )
    ux.console.rule()


def _parse_startup_args():
    """Parse arguments without reading project-owned session state."""
    parser = argparse.ArgumentParser(prog="minicc")
    parser.add_argument(
        "--continue",
        dest="cont",
        action="store_true",
        help="resume the most recent session in this directory",
    )
    parser.add_argument("--resume", metavar="ID",
                        help="resume a specific session id")
    return parser, parser.parse_args()


def _init_session(parser=None, args=None, *, allow_project_state: bool = True):
    """Load startup selection and return ``(history, session_id, resumed)``.

    Explicit resume failures are fatal and descriptive. A missing or corrupt
    transcript must never be mistaken for a valid empty conversation. Restricted
    workspaces start fresh instead of reading repository-owned transcripts.
    """
    if parser is None or args is None:
        parser, args = _parse_startup_args()

    if not allow_project_state:
        if args.resume or args.cont:
            ux.say(
                "session resume is disabled in restricted mode; starting a fresh "
                "conversation",
                style=ux.S_INFO,
            )
        return [], sessions.new_id(), False

    if args.resume:
        try:
            return sessions.load(args.resume), args.resume, True
        except sessions.SessionError as error:
            parser.error(str(error))
    if args.cont:
        sid = sessions.latest_id()
        if sid:
            try:
                return sessions.load(sid), sid, True
            except sessions.SessionError as error:
                parser.error(str(error))
    return [], sessions.new_id(), False


def _setup_history():
    """Load input history so ↑/↓ and Ctrl-R recall queries across runs.
    Also enables bracketed paste (GNU readline 8.1+): the whole paste —
    newlines included — lands atomically in readline's buffer while the tty
    is in readline's raw mode. Without it, everything after the first line
    floods the kernel's CANONICAL input queue (1024 bytes on macOS) and the
    overflow is silently dropped/mangled — dogfood R5 lost a requirement
    line and the out-of-scope constraints of a 2.4KB pasted brief that way."""
    try:
        readline.parse_and_bind("set enable-bracketed-paste on")
    except Exception:
        pass  # editline or old readline: _read_query's drain is the fallback
    histfile = config.ensure_project_dir() / "repl_history"
    try:
        readline.read_history_file(histfile)
    except (FileNotFoundError, OSError):
        pass
    return histfile


def _sanitize(text: str) -> str:
    """Make input safe to persist and send: the tty/readline layer can hand
    back surrogate escapes when bytes didn't decode cleanly (observed live in
    dogfood R5 — a large paste crashed the transcript writer with 'surrogates
    not allowed', killing the session). Re-encode the escapes back to their
    original bytes and re-decode: sequences that were merely split recover
    their real characters; anything irrecoverable becomes U+FFFD (visible in
    the echoed query) instead of a crash three layers later."""
    try:
        text.encode("utf-8")  # fast path: already clean
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _read_query() -> str:
    """Read one query; a multi-line PASTE arrives as ONE query.

    input() returns at the first newline, so a pasted block would shatter —
    line 1 becomes the query and the REST of the paste is silently lost (the
    next permission prompt's stale-stdin flush eats it; R5 scene audit: two
    sessions each show exactly ONE query = the brief's first sentence, zero
    fragment queries — arm A worked off one sentence, twice). CC's TUI handles this via
    bracketed paste; minicc's stdlib REPL gets the burst-drain equivalent:
    after the first line, consume whatever is ALREADY buffered on stdin
    (select, 50ms per chunk). Paste arrives as a burst; typing never does, so
    an interactively typed line is returned unchanged.

    The drain reads RAW BYTES (os.read) and decodes once at the end — a
    multi-byte character split across tty buffer chunks reassembles instead
    of shattering into surrogates at a text-layer read boundary.

    TTY-only on purpose: with piped stdin everything is "already buffered",
    and draining would swallow the whole script — scripted use (tests, e2e
    smokes) keeps the one-line-per-query semantics."""
    first = input("\nQuery: ")
    if not sys.stdin.isatty():
        return _sanitize(first).strip()
    fd = sys.stdin.fileno()
    chunks: list[bytes] = []
    while select.select([sys.stdin], [], [], 0.05)[0]:
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:  # EOF mid-drain
            break
        chunks.append(data)
    text = _sanitize(first)
    if chunks:
        text += "\n" + b"".join(chunks).decode("utf-8", "replace")
    text = text.strip()
    # Paste receipt: multi-line input echoes its size so truncation is visible
    # BEFORE the turn runs (R5: a mangled paste was only noticed post-run).
    if "\n" in text:
        note = f"[paste received: {text.count(chr(10)) + 1} lines, {len(text)} chars]"
        if "�" in text:
            ux.say(
                note + "  ⚠ contains U+FFFD — bytes were LOST in transit; re-paste",
                style=ux.S_ERROR,
            )
        else:
            ux.say(note, style=ux.S_INFO)
    return text


def _friendly_error(e: Exception) -> str:
    """Turn an exception into a clear, actionable line (vs a raw repr)."""
    if isinstance(e, RateLimitError):
        return (
            "rate limited — the API is throttling and retries were exhausted. "
            "Wait a moment, or /compact to shrink the request."
        )
    if isinstance(e, (APIConnectionError, APITimeoutError)):
        return "network error reaching the API — check your connection and retry."
    if isinstance(e, APIStatusError):
        return f"API error {e.status_code}: {getattr(e, 'message', '') or ''}".rstrip(
            ": "
        )
    return f"agent error: {e!r}"


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
    local_grants_trusted: bool = False,
) -> bool:
    """Ask for exact Trust of ``workspace_dir`` and show gated fields."""
    permission_entries = _workspace_trust_permission_entries(
        snapshot,
        include_local=not local_grants_trusted,
    )
    directory_entries = _workspace_trust_directory_entries(
        snapshot,
        include_local=not local_grants_trusted,
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


def _activate_workspace_settings() -> bool:
    """Bind the Trust-filtered settings view for the launch directory.

    A fresh decline continues user-only with project ``deny``/``ask`` rules.
    Parent-covered workspaces activate ordinary project configuration, while an
    exact decision or local-file provenance independently controls grant fields.
    """
    launch_dir = Path.cwd().resolve()
    snapshot = config.discover_settings(launch_dir)
    local_grants_trusted = launch_dir == Path.home().resolve()
    config.activate(
        snapshot.view(
            trusted=False,
            local_grants_trusted=local_grants_trusted,
        )
    )
    manager = trust.TrustManager()
    identity = manager.workspace_identity(launch_dir)
    if manager.is_explicitly_trusted(identity):
        config.activate(snapshot.view(trusted=True))
        return True
    covered_by_parent = manager.is_trusted(identity)
    if covered_by_parent:
        local_grants_trusted = not local_settings_are_repository_supplied(
            launch_dir
        )
        config.activate(
            snapshot.view(
                trusted=True,
                project_grants_trusted=False,
                local_grants_trusted=local_grants_trusted,
            )
        )
        if not (
            _workspace_trust_permission_entries(
                snapshot,
                include_local=not local_grants_trusted,
            )
            or _workspace_trust_directory_entries(
                snapshot,
                include_local=not local_grants_trusted,
            )
        ):
            return True
    if not _confirm_workspace_trust(
        launch_dir,
        identity,
        snapshot,
        covered_by_parent=covered_by_parent,
        local_grants_trusted=local_grants_trusted,
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
    config.activate(snapshot.view(trusted=True))
    return True


def _main():
    parser, args = _parse_startup_args()
    trusted = _activate_workspace_settings()
    history, session_id, resumed = _init_session(
        parser,
        args,
        allow_project_state=trusted,
    )
    llm.configure_from_settings()
    tool_registry.configure_from_settings()
    histfile = _setup_history()
    checkpoints.activate(session_id, resume=resumed)
    hooks.reset()  # load hook config from settings.json for this session
    # ${CLAUDE_SESSION_ID} + fresh already-loaded tracking
    skills.reset(session_id)
    reminders.reset()  # claudeMd/skills reminders inject on the first prompt
    requested = config.allowed_tools()
    # trusted in settings (bash excluded)
    pre_approved = permissions.preload(requested)
    refused = sorted(set(requested) & permissions.NO_PRELOAD)
    # env + git snapshot (layer 2) + SessionStart hook context (fires here; CC
    # sources: "resume" when picking up an existing transcript, else "startup").
    # CLAUDE.md / memory index / skill listing are NOT loaded here: they ride
    # <system-reminder> messages injected at prompt time (reminders.py, CC parity).
    llm.set_session_context(
        _session_context_with_hooks(
            session_id, "resume" if resumed else "startup")
    )
    _print_startup_banner(
        pre_approved,
        refused,
        session_id,
        history,
        resumed=resumed,
    )
    ctx = (
        context_management.ContextState()
    )  # this conversation's trigger state (rotated on /clear)
    turn = checkpoints.last_turn()
    while True:
        try:
            query = _read_query()
        except (EOFError, KeyboardInterrupt):
            break

        if not query:
            continue
        if query.lower() in ("q", "exit", "quit"):
            break
        # Skill allowed-tools grants last until the NEXT message (CC's window) —
        # this input is that message; a skill invoked below re-grants for itself.
        permissions.clear_skill_grants()

        # A slash-command turn expands into CC's two-message shape (probed live;
        # same in B's /init transcript): a command-tags user message + the
        # expansion user message (transcript marks the second `meta`, CC's isMeta).
        # (tags, expanded content) when this turn is an expansion
        expansion = None
        if query.strip() == "/init":
            ux.say("scanning the project to write CLAUDE.md ...", style=ux.S_INFO)
            expansion = (
                "<command-message>init</command-message>\n<command-name>/init</command-name>",
                _INIT_PROMPT,
            )
            # fall through: run as a normal agent turn (tools + streaming + persist)
        elif query.startswith("/"):
            # split into command word + optional argument (e.g. /compact <focus>)
            parts = query.split(maxsplit=1)
            cmd, arg = parts[0], (parts[1] if len(parts) > 1 else None)
            # built-in commands → handlers (closures capture this turn's
            # history/arg/session_id). /clear is special — it rotates the
            # session, so it reassigns session_id/turn instead of joining here.
            builtins = {
                "/help": _cmd_help,
                "/cost": _cmd_cost,
                "/context": lambda: _cmd_context(history, ctx),
                "/model": lambda: _cmd_model(arg),
                "/compact": lambda: _cmd_compact(
                    history, ctx, focus=arg, session_id=session_id
                ),
                "/recap": lambda: _cmd_recap(history),
                "/memory": lambda: _cmd_memory(arg),
                "/rewind": lambda: _cmd_rewind(
                    history, arg, session_id=session_id, ctx=ctx
                ),
            }
            if cmd == "/clear":
                session_id = _cmd_clear(history, session_id)
                ctx = context_management.ContextState()
                turn = 0
            elif cmd in builtins:
                try:
                    builtins[cmd]()
                except hooks.HookStop as error:
                    ux.say(str(error), style=ux.S_ERROR)
            else:
                # not a built-in — try a skill (built-ins win a name clash, like
                # CC's built-in commands; skills can't shadow /clear or /help)
                expansion = skills.user_invoke(cmd[1:], arg or "")
                if expansion is None:
                    ux.say(
                        f"unknown command: {query}  (try /help)", style=ux.S_ERROR)
            if expansion is None:
                continue
            ux.say(f"skill: {cmd[1:]}", style=ux.S_INFO)
            # fall through: the expansion runs as a normal agent turn (the
            # /init pattern) — tools, streaming, transcript persist

        # UserPromptSubmit hook: fires before Claude processes the prompt. It can
        # reject the prompt (block / continue:false) or inject context for this turn.
        # Runs only for real prompts, not slash commands (those are handled above);
        # for an expansion turn it sees the expanded content, as before.
        prompt_hook = hooks.run(
            "UserPromptSubmit",
            session_id=session_id,
            prompt=expansion[1] if expansion else query,
        )
        hooks.surface(prompt_hook)
        # Universal continue:false takes precedence over the event's block output.
        if prompt_hook.stop:
            reason = prompt_hook.stop_reason or "no reason given"
            ux.say(f"prompt processing stopped by hook: {reason}", style=ux.S_ERROR)
            continue
        if prompt_hook.block:
            reason = prompt_hook.reason or "no reason given"
            ux.say(f"prompt blocked by hook: {reason}", style=ux.S_ERROR)
            continue

        turn += 1
        ux.say(f">>> USER (turn {turn})", style=ux.S_USER)

        # roll-back point if this turn is interrupted/errors
        mark = len(history)
        # transcript position BEFORE this turn's user message — the replay target
        # if a later `/rewind N conversation` returns to this turn
        events = sessions.event_count(session_id)
        # <system-reminder> injection (CC's mechanism): claudeMd / skill listing
        # ride INSIDE the user turn, reminder text first (observed shape of CC's
        # own turns). The transcript stores only the typed/expanded text —
        # reminders are ephemeral (CC parity: its session JSONLs carry none),
        # so resume/rewind/compact self-heal via re-injection at the next prompt.
        # Being past `mark`, an interrupted turn rolls them back too.
        prefix = "".join(f"{n}\n" for n in reminders.for_prompt(history))
        if expansion:
            tags, expanded = expansion
            history.append({"role": "user", "content": prefix + tags})
            sessions.append_message(
                session_id, {"role": "user", "content": tags})
            history.append({"role": "user", "content": expanded})
            sessions.append_message(
                session_id, {"role": "user", "content": expanded}, meta=True
            )
        else:
            history.append({"role": "user", "content": prefix + query})
            sessions.append_message(
                session_id, {"role": "user", "content": query})
        # Hook-injected context rides as an extra user message for this turn (CC adds
        # additionalContext to the model's context alongside the prompt).
        for extra_context in prompt_hook.additional_context:
            ctx_msg = {"role": "user", "content": extra_context}
            history.append(ctx_msg)
            sessions.append_message(session_id, ctx_msg)
        # files + conversation anchor
        checkpoints.start(turn, query, events=events)

        try:
            agent_loop(
                history, session_id=session_id, ctx=ctx
            )  # streams; records incrementally
        except hooks.HookStop as error:
            del history[mark:]
            ux.say(str(error), style=ux.S_ERROR)
            continue
        except KeyboardInterrupt:
            # Ctrl-C during a slow tool (e.g. bash) leaves an assistant tool_use
            # with no following tool_result. The next request then 400s:
            # "tool_use ids were found without tool_result blocks" (verified by
            # live test). Roll back the whole turn to a clean state.
            del history[mark:]
            ux.say("interrupted", style=ux.S_INFO)
            continue
        except Exception as e:
            del history[mark:]  # same: don't leave a half-finished turn behind
            ux.say(_friendly_error(e), style=ux.S_ERROR)
            continue
        # No post-loop re-print: streaming already rendered the assistant text.
        # The transcript is written incrementally (sessions.append_message +
        # sessions.log_compaction) as the turn happens — no turn-end save() needed.

    # loop exited (q/exit/EOF/Ctrl-C): SessionEnd hook, then persist input history.
    # All minicc exits leave from the prompt, so the reason is always
    # "prompt_input_exit" (CC's logout/resume/other reasons have no surface here).
    _fire_session_end(session_id, "prompt_input_exit")
    try:
        readline.write_history_file(histfile)
    except OSError:
        pass


def main():
    """Console entry point with a clean boundary for lifecycle-hook stops."""
    try:
        _main()
    except hooks.HookStop as error:
        ux.say(str(error), style=ux.S_ERROR)
    except checkpoints.CheckpointError as error:
        ux.say(str(error), style=ux.S_ERROR)


if __name__ == "__main__":
    main()
