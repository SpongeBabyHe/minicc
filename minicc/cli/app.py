"""Terminal entry point and top-level session/turn orchestration.

The file intentionally keeps startup order, slash-command routing, prompt-time
Hooks, transcript writes, checkpoints, and ``agent_loop`` visible as one REPL
workflow. Command implementations and Workspace Trust activation live in their
own modules so this file describes when work happens rather than every detail.
"""

import argparse
import os
import platform
import readline  # noqa: F401 — importing enables history + line editing for input()
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
from minicc import context_management, llm
from minicc.cli import commands
from minicc.query_engine import agent_loop
from minicc import ux
from minicc import permissions
from minicc import sessions
from minicc import config
from minicc import checkpoints
from minicc import hooks
from minicc import reminders
from minicc import skills
from minicc import tools as tool_registry
from minicc.tools import freshness
from minicc.prompts.init import INIT_PROMPT
from minicc.prompts.system import build_session_context
from minicc.cli.workspace_activation import activate_workspace_settings


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
    ctx = build_session_context(
        include_git=config.current_settings().project_configuration_enabled
    )
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
        "commit": (
            _git_sha()
            if config.current_settings().project_configuration_enabled
            else "restricted"
        ),
        "model": llm.get_model(),
        "cwd": str(Path.cwd()),
        "os": platform.system(),
    }
    if (Path.cwd() / "CLAUDE.md").exists():
        info["CLAUDE.md"] = (
            "found (injected as a system-reminder at first prompt)"
            if config.current_settings().project_configuration_enabled
            else "found (disabled in restricted mode)"
        )
    return info


def _clear_session(history, session_id: str) -> str:
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
    if allow_rules := config.permission_allow_rules():
        ux.say(  # persistent trust stays visible (PERMISSIONS.md principle)
            f"permission allow rules from settings: {', '.join(allow_rules)}",
            style=ux.S_INFO,
        )
    if skill_names := sorted(skills.discover()):
        ux.say(
            f"skills: {', '.join('/' + n for n in skill_names)}", style=ux.S_INFO)
    if refused:
        ux.say(
            f"settings list {', '.join(refused)} for whole-tool pre-approval, "
            "which is ignored; approve bounded rules instead (see PERMISSIONS.md)",
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


def _run_builtin(handler) -> None:
    """Run one slash command without letting a nested model call kill the REPL.

    Commands such as ``/compact``, ``/recap``, and ``/memory consolidate`` call
    the model outside the normal agent-turn rollback boundary. The core loop
    deliberately lets operational failures escape; this is their caller-owned
    UX boundary.
    """
    try:
        handler()
    except hooks.HookStop as error:
        ux.say(str(error), style=ux.S_ERROR)
    except KeyboardInterrupt:
        ux.say("interrupted", style=ux.S_INFO)
    except checkpoints.CheckpointError as error:
        ux.say(str(error), style=ux.S_ERROR)
    except Exception as error:
        ux.say(_friendly_error(error), style=ux.S_ERROR)


def _main():
    parser, args = _parse_startup_args()
    trusted = activate_workspace_settings()
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
                INIT_PROMPT,
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
                "/help": commands.show_help,
                "/cost": commands.show_cost,
                "/context": lambda: commands.show_context(history, ctx),
                "/model": lambda: commands.select_model(arg),
                "/compact": lambda: commands.compact(
                    history, ctx, focus=arg, session_id=session_id
                ),
                "/recap": lambda: commands.recap(history),
                "/memory": lambda: commands.manage_memory(arg),
                "/rewind": lambda: commands.rewind(
                    history, arg, session_id=session_id, context=ctx
                ),
            }
            if cmd == "/clear":
                session_id = _clear_session(history, session_id)
                ctx = context_management.ContextState()
                turn = 0
            elif cmd in builtins:
                _run_builtin(builtins[cmd])
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
            outcome = agent_loop(
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
        if not outcome.completed:
            # Typed non-completion is an expected terminal result, not an
            # exception transaction: keep its valid partial history/transcript
            # so the user can explicitly continue or rephrase.
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
