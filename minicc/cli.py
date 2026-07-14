import argparse
import platform
import readline  # noqa: F401 — importing enables history + line editing for input()
import subprocess
from datetime import datetime
from pathlib import Path
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from minicc import llm
from minicc.agent import agent_loop
from minicc import ux
from minicc.llm import get_usage, context_usage, compact, recap
from minicc import permissions
from minicc import sessions
from minicc import config
from minicc import checkpoints
from minicc import memory
from minicc import hooks
from minicc.prompts.system import load_project_context, build_session_context


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
#   3. batch parallel reads — turn count drove a 3x context-traffic spread.
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
    "productive more quickly. Focus on the \"big picture\" architecture that "
    "requires reading multiple files to understand.\n\n"
    "Usage notes:\n"
    "- VERIFY each command works by actually running it (bash) before documenting "
    "it; if it needs env vars or a .env file, say so next to the command. If you "
    "cannot verify a command, do not present it as working.\n"
    "- Before writing a claim containing \"all\", \"only\", \"both\" or "
    "\"never\", check each member it quantifies over.\n"
    "- Explore with glob/grep/read_file; batch independent file reads as parallel "
    "tool calls in a single turn — each extra turn re-reads the whole context.\n"
    "- If there's already a CLAUDE.md, improve it in place rather than duplicating.\n"
    "- Do not repeat yourself and do not include obvious instructions like "
    "\"Provide helpful error messages to users\", \"Write unit tests for all new "
    "utilities\", \"Never include sensitive information (API keys, tokens) in "
    "code or commits\".\n"
    "- Avoid listing every component or file structure that can be easily "
    "discovered.\n"
    "- Don't include generic development practices.\n"
    "- If there are Cursor rules (in .cursor/rules/ or .cursorrules) or Copilot "
    "rules (in .github/copilot-instructions.md), make sure to include the "
    "important parts.\n"
    "- If there is a README.md, make sure to include the important parts.\n"
    "- Do not make up information such as \"Common Development Tasks\", \"Tips "
    "for Development\", \"Support and Documentation\" unless this is expressly "
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
    """Run SessionStart hooks (source: "startup" | "resume" | "clear" — CC's fourth,
    "compact", has no minicc surface since compaction is in-place, not a session
    restart). Returns hook-injected context to append to the session-context layer
    ("" if none) — CC injects additionalContext into context at session start the
    same way. Cannot block."""
    d = hooks.run(
        "SessionStart",
        session_id=session_id,
        match_value=source,
        source=source,
        model=llm.get_model(),
    )
    for m in d.system_messages:
        ux.say(f"[hook] {m}", style=ux.S_INFO)
    return "\n".join(d.additional_context)


def _session_context_with_hooks(session_id: str, source: str) -> str:
    """The session-context layer (env + git) plus any SessionStart hook context."""
    ctx = build_session_context()
    extra = _fire_session_start(session_id, source)
    if extra:
        ctx += f"\n\n# Context from SessionStart hook\n\n{extra}"
    return ctx


def _fire_session_end(session_id: str, reason: str) -> None:
    """Run SessionEnd hooks (reason: "clear" | "prompt_input_exit" — CC's other
    reasons target absent infra). Informational only: exit codes and decision
    fields are ignored per CC's contract; only systemMessage surfaces."""
    d = hooks.run("SessionEnd", session_id=session_id, match_value=reason, reason=reason)
    for m in d.system_messages:
        ux.say(f"[hook] {m}", style=ux.S_INFO)


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
        "commit": _git_sha(),
        "model": llm.get_model(),
        "cwd": str(Path.cwd()),
        "os": platform.system(),
    }
    if (Path.cwd() / "CLAUDE.md").exists():
        info["CLAUDE.md"] = "loaded"
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
                ("/compact [focus]", "Summarize older history now (optional focus)"),
                ("/recap", "Show a summary without changing history"),
                ("/memory [file|on|off|consolidate]", "Browse, toggle, or tidy cross-session memory"),
                (
                    "/rewind [N] [mode]",
                    "List restore points; restore code (default) | conversation | both",
                ),
                ("q / exit / quit", "Leave minicc"),
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
    cost += u.get("web_searches", 0) * 0.01   # server web_search: $10 per 1,000
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


def _cmd_context(messages):
    """
    Show context token usage vs the compaction budget.
    """
    c = context_usage(messages)
    ux.say(
        ux.kv_block(
            [
                (
                    "context tokens",
                    f"{c['estimated_tokens']:,}  (~{c['pct_of_budget']:.0f}% of compaction budget)",
                ),
                ("compaction budget", f"{c['budget']:,}  (auto-compaction triggers above this)"),
                ("messages", str(c["messages"])),
                ("tool_results", f"{c['tool_results']} total, {c['evicted']} evicted"),
                ("eviction events", str(c["eviction_events"])),
                ("compaction events", str(c["compaction_events"])),
            ]
        )
    )
    ux.say(
        "(estimate covers conversation history only, not system prompt + tools)",
        style=ux.S_INFO,
    )


def _cmd_compact(messages, focus: str | None = None, session_id: str | None = None):
    """Manually compact history (L6b). Mutates `messages` in place."""
    did = compact(messages, focus=focus, session_id=session_id)
    if did:
        ux.say("conversation history compacted", style=ux.S_INFO)
    else:
        ux.say("nothing to compact yet", style=ux.S_INFO)


def _cmd_recap(messages):
    """Show a summary of the conversation without changing it (L6c)."""
    summary = recap(messages)
    ux.say("<<< RECAP (history unchanged)", style=ux.S_ASSISTANT)
    ux.markdown(summary)


def _cmd_memory(arg: str | None):
    """Browse, toggle, or consolidate auto-memory. `/memory` lists the store;
    `/memory <file>` views one file; `/memory on|off` toggles it for this session;
    `/memory consolidate` runs an Auto-Dream-style tidy pass (merge duplicates,
    retire stale facts, fix dates, tighten the index)."""
    if arg in ("on", "off"):
        memory.set_enabled(arg == "on")
        llm.set_memory_index(memory.load_index())  # refresh what's injected
        ux.say(f"auto-memory {'enabled' if arg == 'on' else 'disabled'}", style=ux.S_INFO)
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
        llm.set_memory_index(memory.load_index())  # reload the tidied index
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
    /model default <a|id>  → set the persistent default (global settings) + switch
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
        # always global for now. config.set_default_model supports scope="project"
        # and resolve_model already reads project > global, but there's no command
        # surface (e.g. --project) to write a per-project default yet — deferred.
        config.set_default_model(target)
        llm.set_model(target)
        ux.say(
            f"default model → {target}  (persisted globally + switched)",
            style=ux.S_INFO,
        )
        return

    target = _MODEL_ALIASES.get(arg.strip(), arg.strip())
    llm.set_model(target)
    ux.say(f"model → {target}  (this session)", style=ux.S_INFO)


def _cmd_rewind(history, arg: str | None, session_id: str | None = None):
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
        ux.say(f"unknown mode {mode!r} (code | conversation | both)", style=ux.S_ERROR)
        return
    if not (1 <= n <= len(points)):
        ux.say(f"no restore point [{n}]  (try /rewind to list)", style=ux.S_ERROR)
        return
    turn, query, _ = points[n - 1]
    # read the conversation anchor BEFORE restore_files trims the stack
    events = checkpoints.events_at(turn)

    if mode in ("code", "both"):
        restored, failed = checkpoints.restore_files(turn)
        msg = f"reverted {restored} file change(s) to restore point {n}"
        if failed:
            msg += f"  — {len(failed)} could not be restored: {', '.join(failed)}"
        ux.say(msg, style=ux.S_INFO)

    if mode in ("conversation", "both"):
        rewound = sessions.load_upto(session_id, events) if session_id else None
        if rewound is None:
            ux.say("no transcript to rewind from", style=ux.S_ERROR)
            return
        history[:] = rewound
        sessions.log_rewind(session_id, history)  # append-only: a reset event
        llm.reset_context_size()  # forget the pre-rewind size (avoid spurious compact)
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


def _init_session():
    """Parse --continue/--resume and return (history, session_id)."""
    parser = argparse.ArgumentParser(prog="minicc")
    parser.add_argument(
        "--continue",
        dest="cont",
        action="store_true",
        help="resume the most recent session in this directory",
    )
    parser.add_argument("--resume", metavar="ID", help="resume a specific session id")
    args = parser.parse_args()

    if args.resume:
        return sessions.load(args.resume) or [], args.resume
    if args.cont:
        sid = sessions.latest_id()
        if sid:
            return sessions.load(sid) or [], sid
    return [], sessions.new_id()


def _setup_history():
    """Load input history so ↑/↓ and Ctrl-R recall queries across runs."""
    histfile = Path.cwd() / ".minicc" / "repl_history"
    histfile.parent.mkdir(parents=True, exist_ok=True)
    gi = histfile.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    try:
        readline.read_history_file(histfile)
    except (FileNotFoundError, OSError):
        pass
    return histfile


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


def main():
    history, session_id = _init_session()
    histfile = _setup_history()
    checkpoints.reset()  # clear stale checkpoint dirs from a prior session (no cross-restart load yet)
    hooks.reset()  # load hook config from settings.json for this session
    requested = config.allowed_tools()
    pre_approved = permissions.preload(requested)  # trusted in settings (bash excluded)
    refused = sorted(set(requested) & permissions.NO_PRELOAD)
    llm.set_project_context(load_project_context())
    # env + git snapshot (layer 3) + SessionStart hook context (fires here; CC
    # sources: "resume" when picking up an existing transcript, else "startup")
    llm.set_session_context(
        _session_context_with_hooks(session_id, "resume" if history else "startup")
    )
    llm.set_memory_index(memory.load_index())          # auto-memory index (rides layer 2)
    ux.console.rule()
    ux.say(ux.kv_block(list(_session_info().items()), indent=""), style=ux.S_INFO)
    if pre_approved:
        ux.say(
            f"pre-approved (no prompt) from settings: {', '.join(sorted(pre_approved))}",
            style=ux.S_INFO,
        )
    bash_rules = config.permission_allow_rules()
    if bash_rules:
        ux.say(  # persistent trust stays visible (PERMISSIONS.md principle)
            f"bash allow rules from settings: {', '.join(bash_rules)}",
            style=ux.S_INFO,
        )
    if refused:
        ux.say(
            f"settings list {', '.join(refused)} but it can't be pre-approved "
            "(approve per session — see PERMISSIONS.md)",
            style=ux.S_INFO,
        )
    if history:
        ux.say(
            f"resumed session {session_id} ({len(history)} messages)", style=ux.S_INFO
        )
    ux.console.rule()
    turn = 0
    while True:
        try:
            query = input("\nQuery: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not query:
            continue
        if query.lower() in ("q", "exit", "quit"):
            break

        if query.strip() == "/init":
            ux.say("scanning the project to write CLAUDE.md ...", style=ux.S_INFO)
            query = _INIT_PROMPT
            # fall through: run as a normal agent turn (tools + streaming + persist)
        elif query.startswith("/"):
            # split into command word + optional argument (e.g. /compact <focus>)
            parts = query.split(maxsplit=1)
            cmd = parts[0]
            arg = parts[1] if len(parts) > 1 else None
            if cmd == "/help":
                _cmd_help()
            elif cmd == "/clear":
                _fire_session_end(session_id, "clear")  # old session ends (CC reason "clear")
                history.clear()
                session_id = sessions.new_id()  # fresh transcript; old one kept on disk
                llm.reset_context_size()  # stale size would trigger a spurious compact
                permissions.reset()
                permissions.preload(
                    config.allowed_tools()
                )  # keep settings-trusted tools
                checkpoints.reset()
                hooks.reset()  # re-read hook config (settings may have changed)
                turn = 0
                llm.set_project_context(load_project_context())  # reload CLAUDE.md
                # refresh env/git + SessionStart(source="clear") hook context
                llm.set_session_context(_session_context_with_hooks(session_id, "clear"))
                llm.set_memory_index(memory.load_index())         # reload memory index
                ux.say(
                    "conversation, permissions reset; CLAUDE.md reloaded",
                    style=ux.S_INFO,
                )
            elif cmd == "/cost":
                _cmd_cost()
            elif cmd == "/model":
                _cmd_model(arg)
            elif cmd == "/context":
                _cmd_context(history)
            elif cmd == "/compact":
                _cmd_compact(history, focus=arg, session_id=session_id)
            elif cmd == "/recap":
                _cmd_recap(history)
            elif cmd == "/memory":
                _cmd_memory(arg)
            elif cmd == "/rewind":
                _cmd_rewind(history, arg, session_id=session_id)
            else:
                ux.say(f"unknown command: {query}  (try /help)", style=ux.S_ERROR)
            continue

        # UserPromptSubmit hook: fires before Claude processes the prompt. It can
        # reject the prompt (block / continue:false) or inject context for this turn.
        # Runs only for real prompts, not slash commands (those are handled above).
        prompt_hook = hooks.run("UserPromptSubmit", session_id=session_id, user_prompt=query)
        for m in prompt_hook.system_messages:
            ux.say(f"[hook] {m}", style=ux.S_INFO)
        if prompt_hook.block or prompt_hook.stop:
            reason = prompt_hook.reason or prompt_hook.stop_reason or "no reason given"
            ux.say(f"prompt blocked by hook: {reason}", style=ux.S_ERROR)
            continue

        turn += 1
        ux.say(f">>> USER (turn {turn})", style=ux.S_USER)

        mark = len(history)  # roll-back point if this turn is interrupted/errors
        # transcript position BEFORE this turn's user message — the replay target
        # if a later `/rewind N conversation` returns to this turn
        events = sessions.event_count(session_id)
        user_msg = {"role": "user", "content": query}
        history.append(user_msg)
        sessions.append_message(session_id, user_msg)  # append-only transcript
        # Hook-injected context rides as an extra user message for this turn (CC adds
        # additionalContext to the model's context alongside the prompt).
        for ctx in prompt_hook.additional_context:
            ctx_msg = {"role": "user", "content": ctx}
            history.append(ctx_msg)
            sessions.append_message(session_id, ctx_msg)
        checkpoints.start(turn, query, events=events)  # files + conversation anchor

        try:
            agent_loop(history, session_id=session_id)  # streams; records incrementally
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
        # llm.log_compaction) as the turn happens — no turn-end save() needed.

    # loop exited (q/exit/EOF/Ctrl-C): SessionEnd hook, then persist input history.
    # All minicc exits leave from the prompt, so the reason is always
    # "prompt_input_exit" (CC's logout/resume/other reasons have no surface here).
    _fire_session_end(session_id, "prompt_input_exit")
    try:
        readline.write_history_file(histfile)
    except OSError:
        pass


if __name__ == "__main__":
    main()
