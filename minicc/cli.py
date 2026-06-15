import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from minicc import llm
from minicc.agent import agent_loop
from minicc import ux
from minicc.llm import get_usage, context_usage
from minicc import permissions
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
        "model": os.environ.get("MODEL_ID", "?"),
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


def main():
    llm.set_project_context(load_project_context())
    ux.console.rule()
    ux.say(ux.kv_block(list(_session_info().items()), indent=""), style=ux.S_INFO)
    ux.console.rule()
    history = []
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
            if query == "/help":
                _cmd_help()
            elif query == "/clear":
                history.clear()
                permissions.reset()
                turn = 0
                llm.set_project_context(load_project_context())  # reload CLAUDE.md
                ux.say(
                    "conversation, permissions reset; CLAUDE.md reloaded",
                    style=ux.S_INFO,
                )
            elif query == "/cost":
                _cmd_cost()
            elif query == "/context":
                _cmd_context(history)
            else:
                ux.say(f"unknown command: {query}  (try /help)", style=ux.S_ERROR)
            continue

        turn += 1
        ux.say(f">>> USER (turn {turn})", style=ux.S_USER)

        history.append({"role": "user", "content": query})

        try:
            agent_loop(history)
        except KeyboardInterrupt:
            ux.say("interrupted", style=ux.S_INFO)
            continue
        except Exception as e:
            ux.say(f"agent error: {e!r}", style=ux.S_ERROR)
            continue

        last_content = history[-1]["content"]
        if isinstance(last_content, list):
            text = "\n".join(b.text for b in last_content if hasattr(b, "text"))
            if text:
                ux.say("<<< ASSISTANT", style=ux.S_ASSISTANT)
                ux.markdown(text)


if __name__ == "__main__":
    main()
