import argparse
import platform
import readline  # noqa: F401 — importing enables history + line editing for input()
import subprocess
from datetime import datetime
from pathlib import Path
from anthropic import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from minicc import llm
from minicc.agent import agent_loop
from minicc import ux
from minicc.llm import get_usage, context_usage, compact, recap
from minicc import permissions
from minicc import sessions
from minicc import config
from minicc.prompts.system import load_project_context


# Sonnet 4.6 pricing (USD per 1M tokens). Update if you switch models.
_PRICE_INPUT_PER_M = 3.0
_PRICE_OUTPUT_PER_M = 15.0
_PRICE_CACHE_WRITE_PER_M = 3.75  # 1.25x input
_PRICE_CACHE_READ_PER_M = 0.30  # 0.1x input


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
                ("/context", "Show conversation token usage vs eviction budget"),
                ("/cost", "Show token usage and estimated cost"),
                ("/model [default] [id]", "Show / switch session / set persistent default model"),
                ("/compact [focus]", "Summarize older history now (optional focus)"),
                ("/recap", "Show a summary without changing history"),
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
    total_in = u["input"] + u["cache_read"] + u["cache_creation"]
    hit = (u["cache_read"] / total_in * 100) if total_in else 0
    ux.say(
        ux.kv_block(
            [
                ("uncached input", f"{u['input']:,}"),
                ("cache read", f"{u['cache_read']:,}  ({hit:.0f}% hit rate)"),
                ("cache write", f"{u['cache_creation']:,}"),
                ("output", f"{u['output']:,}"),
                ("est. cost", f"${cost:.4f}"),
            ]
        )
    )


def _cmd_context(messages):
    """
    Show conversation history token usage vs L3 evict budget.
    """
    c = context_usage(messages)
    ux.say(
        ux.kv_block(
            [
                (
                    "estimated tokens",
                    f"{c['estimated_tokens']:,}  (~{c['pct_of_budget']:.0f}% of evict budget)",
                ),
                ("evict budget", f"{c['budget']:,}  (L3 eviction triggers above this)"),
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


def _cmd_compact(messages, focus: str | None = None):
    """Manually compact history (L6b). Mutates `messages` in place."""
    did = compact(messages, focus=focus)
    if did:
        ux.say("conversation history compacted", style=ux.S_INFO)
    else:
        ux.say("nothing to compact yet", style=ux.S_INFO)


def _cmd_recap(messages):
    """Show a summary of the conversation without changing it (L6c)."""
    summary = recap(messages)
    ux.say("<<< RECAP (history unchanged)", style=ux.S_ASSISTANT)
    ux.markdown(summary)


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
        rows = [("current (session)", cur), ("default (persisted)", config.resolve_model())]
        rows += [(alias, mid) for alias, mid in _MODEL_ALIASES.items()]
        ux.say(ux.kv_block(rows))
        ux.say("usage: /model <alias|id>  ·  /model default <alias|id>", style=ux.S_INFO)
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
        ux.say(f"default model → {target}  (persisted globally + switched)", style=ux.S_INFO)
        return

    target = _MODEL_ALIASES.get(arg.strip(), arg.strip())
    llm.set_model(target)
    ux.say(f"model → {target}  (this session)", style=ux.S_INFO)


def _init_session():
    """Parse --continue/--resume and return (history, session_id)."""
    parser = argparse.ArgumentParser(prog="minicc")
    parser.add_argument(
        "--continue", dest="cont", action="store_true",
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
        return f"API error {e.status_code}: {getattr(e, 'message', '') or ''}".rstrip(": ")
    return f"agent error: {e!r}"


def main():
    history, session_id = _init_session()
    histfile = _setup_history()
    llm.set_project_context(load_project_context())
    ux.console.rule()
    ux.say(ux.kv_block(list(_session_info().items()), indent=""), style=ux.S_INFO)
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

        if query.startswith("/"):
            # split into command word + optional argument (e.g. /compact <focus>)
            parts = query.split(maxsplit=1)
            cmd = parts[0]
            arg = parts[1] if len(parts) > 1 else None
            if cmd == "/help":
                _cmd_help()
            elif cmd == "/clear":
                history.clear()
                permissions.reset()
                turn = 0
                llm.set_project_context(load_project_context())  # reload CLAUDE.md
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
                _cmd_compact(history, focus=arg)
            elif cmd == "/recap":
                _cmd_recap(history)
            else:
                ux.say(f"unknown command: {query}  (try /help)", style=ux.S_ERROR)
            continue

        turn += 1
        ux.say(f">>> USER (turn {turn})", style=ux.S_USER)

        mark = len(history)   # roll-back point if this turn is interrupted/errors
        history.append({"role": "user", "content": query})

        try:
            agent_loop(history)   # streams assistant text to the screen as it arrives
        except KeyboardInterrupt:
            # Ctrl-C during a slow tool (e.g. bash) leaves an assistant tool_use
            # with no following tool_result. The next request then 400s:
            # "tool_use ids were found without tool_result blocks" (verified by
            # live test). Roll back the whole turn to a clean state.
            del history[mark:]
            ux.say("interrupted", style=ux.S_INFO)
            continue
        except Exception as e:
            del history[mark:]   # same: don't leave a half-finished turn behind
            ux.say(_friendly_error(e), style=ux.S_ERROR)
            continue
        # No post-loop re-print: streaming already rendered the assistant text.

        # persist after each successful turn so --continue/--resume can pick up here
        sessions.save(session_id, history, llm.get_model())

    # loop exited (q/exit/EOF/Ctrl-C): persist input history for next run
    try:
        readline.write_history_file(histfile)
    except OSError:
        pass


if __name__ == "__main__":
    main()
