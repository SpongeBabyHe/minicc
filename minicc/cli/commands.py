"""Built-in slash-command handlers for the terminal interface.

The CLI keeps command routing and session rotation visible in its main loop.
Handlers here perform one command's work against explicitly supplied runtime
state; none decides whether the current input should continue into an agent turn.
"""

from minicc import checkpoints, config, context_management, llm, memory, reminders
from minicc import sessions, skills, ux
from minicc.llm import get_usage


# Sonnet 4.6 pricing (USD per 1M tokens). Update if you switch models.
_PRICE_INPUT_PER_M = 3.0
_PRICE_OUTPUT_PER_M = 15.0
_PRICE_CACHE_WRITE_PER_M = 3.75
_PRICE_CACHE_READ_PER_M = 0.30

# Short aliases for ergonomics; /model also accepts any raw model id.
_MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "fable": "claude-fable-5",
}


def show_help() -> None:
    """Display built-in commands plus currently discoverable Skills."""
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
            + [
                (
                    f"/{name}" + (f" {skill.hint}" if skill.hint else ""),
                    f"skill: {skill.description[:100]}",
                )
                for name, skill in sorted(skills.discover().items())
                if skill.meta.get("user-invocable") is not False
            ]
        )
    )


def show_cost() -> None:
    """Display cumulative API usage and the local price estimate."""
    usage = get_usage()
    cost = (
        usage["input"] * _PRICE_INPUT_PER_M
        + usage["output"] * _PRICE_OUTPUT_PER_M
        + usage["cache_read"] * _PRICE_CACHE_READ_PER_M
        + usage["cache_creation"] * _PRICE_CACHE_WRITE_PER_M
    ) / 1_000_000
    cost += usage.get("web_searches", 0) * 0.01
    total_input = usage["input"] + usage["cache_read"] + usage["cache_creation"]
    cache_hit = usage["cache_read"] / total_input * 100 if total_input else 0
    ux.say(
        ux.kv_block(
            [
                ("uncached input", f"{usage['input']:,}"),
                (
                    "cache read",
                    f"{usage['cache_read']:,}  ({cache_hit:.0f}% hit rate)",
                ),
                ("cache write", f"{usage['cache_creation']:,}"),
                ("output", f"{usage['output']:,}"),
                ("web searches", f"{usage.get('web_searches', 0):,}"),
                ("est. cost", f"${cost:.4f}"),
            ]
        )
    )


def show_context(messages, context) -> None:
    """Display working-context usage and compaction statistics."""
    usage = context_management.context_usage(
        context,
        messages,
        model=llm.get_model(),
    )
    ux.say(
        ux.kv_block(
            [
                (
                    "context tokens",
                    f"{usage['estimated_tokens']:,}  "
                    f"(~{usage['pct_of_budget']:.0f}% of compaction budget)",
                ),
                (
                    "compaction budget",
                    f"{usage['budget']:,}  (auto-compaction triggers above this)",
                ),
                ("messages", str(usage["messages"])),
                (
                    "tool_results",
                    f"{usage['tool_results']} total, "
                    f"{usage['evicted']} cleared in working set",
                ),
                ("eviction events", str(usage["eviction_events"])),
                ("results cleared", str(usage["evicted_tool_results"])),
                ("tokens reclaimed", f"~{usage['evicted_tokens']:,}"),
                (
                    "last invalidated suffix",
                    f"~{usage['last_eviction_suffix_tokens']:,} tokens",
                ),
                ("compaction events", str(usage["compaction_events"])),
            ]
        )
    )
    ux.say(
        "(last API total + estimated full-request delta; cold reads include system/tools)",
        style=ux.S_INFO,
    )


def compact(
    messages,
    context,
    focus: str | None = None,
    session_id: str | None = None,
) -> None:
    """Manually compact history in place."""
    compacted = context_management.compact(
        context,
        messages,
        focus=focus,
        session_id=session_id,
        trigger="manual",
        runtime=llm.summary_runtime(),
    )
    if compacted:
        ux.say("conversation history compacted", style=ux.S_INFO)
    elif compacted is False:
        ux.say("nothing to compact yet", style=ux.S_INFO)


def recap(messages) -> None:
    """Show a summary without changing conversation history."""
    summary = context_management.recap(
        messages,
        runtime=llm.summary_runtime(),
    )
    ux.say("<<< RECAP (history unchanged)", style=ux.S_ASSISTANT)
    ux.markdown(summary)


def manage_memory(argument: str | None) -> None:
    """Browse, toggle, or consolidate cross-session memory."""
    if argument in ("on", "off"):
        memory.set_enabled(argument == "on")
        reminders.invalidate()
        ux.say(
            f"auto-memory {'enabled' if argument == 'on' else 'disabled'}",
            style=ux.S_INFO,
        )
        return
    if argument == "consolidate":
        if not memory.enabled():
            ux.say("auto-memory is off (/memory on first)", style=ux.S_ERROR)
            return
        ux.say(
            "[consolidating memory — writes will ask for approval; "
            "answer 'all' to approve the batch]",
            style=ux.S_INFO,
        )
        ux.markdown(memory.consolidate())
        return
    if argument:
        path = argument if argument.startswith("/memories") else f"/memories/{argument}"
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


def select_model(argument: str | None) -> None:
    """Show, switch, or persist the selected model."""
    if not argument:
        rows = [
            ("current (session)", llm.get_model()),
            ("default (persisted)", config.resolve_model()),
        ]
        rows += [(alias, model_id) for alias, model_id in _MODEL_ALIASES.items()]
        ux.say(ux.kv_block(rows))
        ux.say(
            "usage: /model <alias|id>  ·  /model default <alias|id>",
            style=ux.S_INFO,
        )
        return

    parts = argument.split(maxsplit=1)
    if parts[0] == "default":
        if len(parts) < 2:
            ux.say("usage: /model default <alias|id>", style=ux.S_ERROR)
            return
        target = _MODEL_ALIASES.get(parts[1].strip(), parts[1].strip())
        config.set_default_model(target)
        llm.set_model(target)
        ux.say(
            f"default model → {target}  (persisted for this user + switched)",
            style=ux.S_INFO,
        )
        return

    target = _MODEL_ALIASES.get(argument.strip(), argument.strip())
    llm.set_model(target)
    ux.say(f"model → {target}  (this session)", style=ux.S_INFO)


def rewind(
    history,
    argument: str | None,
    session_id: str | None = None,
    context=None,
) -> None:
    """List restore points or restore code, conversation, or both."""
    points = checkpoints.restore_points()
    if argument is None:
        if not points:
            ux.say("nothing to rewind yet", style=ux.S_INFO)
            return
        rows = []
        for index, (_, query, paths) in enumerate(points, 1):
            marker = f"  [{len(paths)} file(s)]" if paths else ""
            rows.append((f"[{index}]", ux.truncate(query, 60) + marker))
        ux.say(ux.kv_block(rows))
        ux.say(
            "usage: /rewind <n> [code|conversation|both] — code (default) reverts "
            "files, conversation replays history back to before that prompt; "
            "bash-made changes aren't tracked.",
            style=ux.S_INFO,
        )
        return

    parts = argument.split()
    mode = parts[1] if len(parts) > 1 else "code"
    try:
        position = int(parts[0])
    except ValueError:
        ux.say("usage: /rewind <n> [code|conversation|both]", style=ux.S_ERROR)
        return
    if mode not in ("code", "conversation", "both"):
        ux.say(
            f"unknown mode {mode!r} (code | conversation | both)",
            style=ux.S_ERROR,
        )
        return
    if not 1 <= position <= len(points):
        ux.say(
            f"no restore point [{position}]  (try /rewind to list)",
            style=ux.S_ERROR,
        )
        return

    turn, query, _paths = points[position - 1]
    events = checkpoints.events_at(turn)
    if mode in ("code", "both"):
        restored, failed = checkpoints.restore_files(turn)
        message = f"reverted {restored} file change(s) to restore point {position}"
        if failed:
            message += (
                f"  — {len(failed)} could not be restored; "
                f"checkpoint retained for retry: {', '.join(failed)}"
            )
        ux.say(message, style=ux.S_INFO)

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
        sessions.log_rewind(session_id, history)
        if context:
            context.reset_size()
        ux.say(
            f"conversation rewound to before turn {turn}; "
            f"its prompt was: {ux.truncate(query, 80)}",
            style=ux.S_INFO,
        )
    elif mode == "code":
        notice = {
            "role": "user",
            "content": (
                "[Files were rewound to an earlier checkpoint; "
                "edits made since then are undone.]"
            ),
        }
        history.append(notice)
        if session_id:
            sessions.append_message(session_id, notice)
