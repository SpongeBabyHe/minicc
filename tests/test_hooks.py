"""Tests for the event-hook mechanism (minicc/hooks.py).

These exercise the CC-faithful contract directly — matcher semantics, the exit-code
protocol (0=JSON stdout, 2=block, other=non-blocking), and each JSON decision field —
by running real command hooks, so the doc'd behavior and the code can't drift.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import hooks
from minicc.context_management import manager, summary


@pytest.fixture(autouse=True)
def _clear_cache():
    hooks.reset()
    yield
    hooks.reset()


def _use(events, disabled=False, *, monkeypatch):
    """Point the hook loader at a crafted config for one test."""
    monkeypatch.setattr(hooks.config, "load_hooks", lambda: (events, disabled))
    hooks.reset()


def _group(command, matcher="*", **entry):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command, **entry}]}


# ─── matcher semantics (CC: */""/None = all; name/list = exact; else regex) ──

def test_match_all():
    assert hooks._match("*", "bash")
    assert hooks._match("", "bash")
    assert hooks._match(None, "bash")


def test_match_exact_and_list():
    assert hooks._match("bash", "bash")
    assert not hooks._match("bash", "edit_file")
    assert hooks._match("edit_file|write_file", "write_file")
    assert hooks._match("edit_file, write_file", "edit_file")
    assert not hooks._match("edit_file|write_file", "bash")


def test_match_regex_and_bad_regex():
    assert hooks._match("mcp__.*", "mcp__memory__get")   # regex path
    assert not hooks._match("^edit", "write_file")
    assert hooks._match("^write", "write_file")
    assert not hooks._match("(unclosed", "anything")     # malformed → matches nothing


# ─── no hooks / disabled → a no-op Decision ─────────────────────────────────

def test_no_hooks_is_noop(monkeypatch):
    _use({}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={})
    assert not d.block and not d.ask and not d.allow
    assert d.updated_input is None and d.additional_context == []


def test_disable_all_short_circuits(monkeypatch):
    events = {"PreToolUse": [_group("echo blocked >&2; exit 2")]}
    _use(events, disabled=True, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={})
    assert not d.block  # disableAllHooks wins before anything runs


# ─── exit-code protocol ──────────────────────────────────────────────────────

def test_exit_2_blocks_with_stderr_reason(monkeypatch):
    _use({"PreToolUse": [_group("echo 'rm is forbidden' >&2; exit 2")]}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={})
    assert d.block
    assert d.reason == "rm is forbidden"


def test_other_nonzero_is_nonblocking_user_message(monkeypatch):
    _use({"PreToolUse": [_group("echo 'hook warning' >&2; exit 1")]}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={})
    assert not d.block  # exit 1 must NOT block
    assert d.system_messages == ["hook warning"]


@pytest.mark.parametrize(
    "event",
    ["PostCompact", "SessionStart", "SessionEnd"],
)
def test_exit_2_is_user_only_for_nonblockable_lifecycle_events(
    monkeypatch,
    event,
):
    _use(
        {event: [_group("echo 'lifecycle warning' >&2; exit 2")]},
        monkeypatch=monkeypatch,
    )

    decision = hooks.run(event)

    assert not decision.block
    assert decision.reason is None
    assert decision.system_messages == ["lifecycle warning"]


def test_large_hook_context_is_capped_and_spilled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd = f"""{sys.executable} -c "print('X' * 10050)" """
    _use(
        {"SessionStart": [_group(cmd)]},
        monkeypatch=monkeypatch,
    )

    decision = hooks.run("SessionStart", session_id="s1")

    assert len(decision.additional_context) == 1
    bounded = decision.additional_context[0]
    assert len(bounded) == hooks._OUTPUT_CHAR_LIMIT
    marker = "Full hook output saved to: "
    assert marker in bounded
    output_path = Path(bounded.split(marker, 1)[1])
    assert output_path.read_text() == "X" * 10_050


# ─── JSON stdout decision control (exit 0) ──────────────────────────────────

def test_pretooluse_deny_json(monkeypatch):
    cmd = """echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"policy"}}'"""
    _use({"PreToolUse": [_group(cmd)]}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={})
    assert d.block and d.reason == "policy"


def test_pretooluse_allow_and_ask(monkeypatch):
    _use({"PreToolUse": [_group("""echo '{"hookSpecificOutput":{"permissionDecision":"allow"}}'""")]}, monkeypatch=monkeypatch)
    assert hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={}).allow

    _use({"PreToolUse": [_group("""echo '{"hookSpecificOutput":{"permissionDecision":"ask"}}'""")]}, monkeypatch=monkeypatch)
    assert hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={}).ask


def test_pretooluse_updated_input(monkeypatch):
    cmd = """echo '{"hookSpecificOutput":{"updatedInput":{"command":"ls -la"}}}'"""
    _use({"PreToolUse": [_group(cmd)]}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={"command": "ls"})
    assert d.updated_input == {"command": "ls -la"}


def test_posttooluse_additional_context_and_updated_output(monkeypatch):
    cmd = """echo '{"hookSpecificOutput":{"additionalContext":"lint clean","updatedToolOutput":{"type":"text","text":"[redacted]"}}}'"""
    _use({"PostToolUse": [_group(cmd)]}, monkeypatch=monkeypatch)
    d = hooks.run("PostToolUse", match_value="edit_file", tool_name="edit_file", tool_input={})
    assert d.additional_context == ["lint clean"]
    assert d.updated_output == "[redacted]"


def test_generic_decision_block(monkeypatch):
    cmd = """echo '{"decision":"block","reason":"tests still red"}'"""
    _use({"Stop": [_group(cmd)]}, monkeypatch=monkeypatch)
    d = hooks.run("Stop", match_value="")
    assert d.block and d.reason == "tests still red"


def test_continue_false_stops(monkeypatch):
    cmd = """echo '{"continue":false,"stopReason":"halt"}'"""
    _use({"UserPromptSubmit": [_group(cmd)]}, monkeypatch=monkeypatch)
    d = hooks.run("UserPromptSubmit", prompt="hi")
    assert d.stop and d.stop_reason == "halt"


def test_plain_stdout_is_context_for_prompt_and_session_start(monkeypatch):
    for event, payload in (
        ("UserPromptSubmit", {"prompt": "hi"}),
        ("SessionStart", {"source": "startup"}),
    ):
        _use({event: [_group("printf 'plain context'")]}, monkeypatch=monkeypatch)
        d = hooks.run(event, **payload)
        assert d.additional_context == ["plain context"]


def test_plain_stdout_is_ignored_for_other_events(monkeypatch):
    _use({"PreToolUse": [_group("printf 'not context'")]}, monkeypatch=monkeypatch)
    d = hooks.run(
        "PreToolUse",
        match_value="bash",
        tool_name="bash",
        tool_input={},
    )
    assert d.additional_context == []


# ─── matcher gates which hooks fire ─────────────────────────────────────────

def test_matcher_filters_by_tool_name(monkeypatch):
    _use({"PreToolUse": [_group("echo x >&2; exit 2", matcher="write_file")]}, monkeypatch=monkeypatch)
    # a bash call must NOT trip a write_file-scoped hook
    assert not hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={}).block
    # ... but a write_file call does
    assert hooks.run("PreToolUse", match_value="write_file", tool_name="write_file", tool_input={}).block


# ─── the hook receives CC's stdin payload ───────────────────────────────────

def test_stdin_payload_reaches_hook(tmp_path, monkeypatch):
    script = tmp_path / "echo_payload.py"
    script.write_text(
        "import sys, json\n"
        "d = json.load(sys.stdin)\n"
        "assert d['permission_mode'] == 'default'\n"
        'ctx = d["tool_name"] + "|" + d["hook_event_name"] + "|" + d["cwd"] + "|" + d["tool_use_id"]\n'
        'print(json.dumps({"hookSpecificOutput": {"additionalContext": ctx}}))\n'
    )
    _use({"PreToolUse": [_group(f"{sys.executable} {script}")]}, monkeypatch=monkeypatch)
    d = hooks.run(
        "PreToolUse",
        match_value="bash",
        tool_name="bash",
        tool_input={"command": "ls"},
        tool_use_id="tu1",
    )
    assert len(d.additional_context) == 1
    tool_name, event, cwd, tool_use_id = d.additional_context[0].split("|")
    assert tool_name == "bash" and event == "PreToolUse" and cwd
    assert tool_use_id == "tu1"


def test_user_prompt_submit_payload_uses_official_prompt_field(tmp_path, monkeypatch):
    script = tmp_path / "prompt_payload.py"
    script.write_text(
        "import sys, json\n"
        "d = json.load(sys.stdin)\n"
        "assert d['prompt'] == 'hello'\n"
        "assert 'user_prompt' not in d\n"
        "print('ok')\n"
    )
    _use(
        {"UserPromptSubmit": [_group(f"{sys.executable} {script}")]},
        monkeypatch=monkeypatch,
    )
    d = hooks.run("UserPromptSubmit", prompt="hello")
    assert d.additional_context == ["ok"]


# ─── timeout is non-blocking, surfaced to the user ──────────────────────────

def test_timeout_is_reported_not_fatal(monkeypatch):
    _use({"PreToolUse": [_group("sleep 2", timeout=1)]}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={})
    assert not d.block
    assert d.system_messages and "timed out" in d.system_messages[0]


# ─── two hooks: deny wins, later hook can't un-block ─────────────────────────

def test_deny_is_sticky_across_hooks(monkeypatch):
    deny = {"matcher": "*", "hooks": [
        {"type": "command", "command": "echo no >&2; exit 2"},
        {"type": "command", "command": """echo '{"hookSpecificOutput":{"permissionDecision":"allow"}}'"""},
    ]}
    _use({"PreToolUse": [deny]}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={})
    assert d.block  # a later allow does not override an earlier deny


# ─── config merge: global + project groups both fire ────────────────────────

def test_load_hooks_merges_global_and_project(tmp_path, monkeypatch):
    import json as _json
    from minicc import config

    g = tmp_path / "global.json"
    p = tmp_path / "project.json"
    local = tmp_path / "local.json"
    g.write_text(_json.dumps({"hooks": {"PreToolUse": [_group("echo g")]}}))
    p.write_text(_json.dumps({"hooks": {"PreToolUse": [_group("echo p")]}, "disableAllHooks": False}))
    local.write_text(_json.dumps({"hooks": {"PreToolUse": [_group("echo local")]}}))
    monkeypatch.setattr(config, "_global", lambda: g)
    monkeypatch.setattr(config, "_project", lambda: p)
    monkeypatch.setattr(config, "_local", lambda: local)

    events, disabled = config.load_hooks()
    assert not disabled
    assert len(events["PreToolUse"]) == 3  # concatenated, all sources fire


def test_load_hooks_disable_uses_scalar_precedence(tmp_path, monkeypatch):
    import json as _json
    from minicc import config

    g = tmp_path / "global.json"
    p = tmp_path / "project.json"
    local = tmp_path / "local.json"
    g.write_text(_json.dumps({"disableAllHooks": True}))
    p.write_text(_json.dumps({"disableAllHooks": False}))
    local.write_text(_json.dumps({"disableAllHooks": True}))
    monkeypatch.setattr(config, "_global", lambda: g)
    monkeypatch.setattr(config, "_project", lambda: p)
    monkeypatch.setattr(config, "_local", lambda: local)

    _events, disabled = config.load_hooks()
    assert disabled

    local.write_text(_json.dumps({"disableAllHooks": False}))
    _events, disabled = config.load_hooks()
    assert not disabled


# ─── wiring: PreCompact / PostCompact around manager.compact ────────────────

def _alternating(n):
    """n messages of valid user/assistant alternation (odd indices = assistant),
    long enough for _find_cut_index to find a safe cut."""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


def test_precompact_block_stops_compaction_before_llm(monkeypatch):
    from minicc import context_management as compact

    _use({"PreCompact": [_group("echo 'not now' >&2; exit 2", matcher="manual")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        summary,
        "_summarize",
        lambda *a, **k: pytest.fail("summary LLM call ran despite block"),
    )
    msgs = _alternating(10)
    before = list(msgs)
    assert compact.compact(compact.ContextState(), msgs, trigger="manual") is None  # vetoed
    assert msgs == before  # history untouched


def test_manual_compact_veto_has_no_misleading_nothing_message(monkeypatch):
    from minicc import cli, context_management as compact, ux

    monkeypatch.setattr(compact, "compact", lambda *args, **kwargs: None)
    said = []
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))

    cli._cmd_compact([], compact.ContextState(), session_id="s1")
    assert not any("nothing to compact" in message for message in said)


def test_postcompact_fires_with_official_payload(tmp_path, monkeypatch):
    from minicc import context_management as compact

    out = tmp_path / "post.txt"
    cmd = (
        f"{sys.executable} -c \"import sys,json,pathlib; "
        f"d=json.load(sys.stdin); "
        f"pathlib.Path(r'{out}').write_text(d['trigger']+'|'+d['compact_summary'])\""
    )
    _use({"PostCompact": [_group(cmd, matcher="manual")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(summary, "_summarize", lambda *a, **k: "SUMMARY")
    msgs = _alternating(10)
    assert compact.compact(compact.ContextState(), msgs, trigger="manual") is True
    assert msgs[0]["role"] == "user" and "SUMMARY" in msgs[0]["content"]
    assert out.read_text() == "manual|SUMMARY"


def test_precompact_auto_matcher_skips_manual_compact(monkeypatch):
    from minicc import context_management as compact

    _use({"PreCompact": [_group("exit 2", matcher="auto")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(summary, "_summarize", lambda *a, **k: "SUMMARY")
    msgs = _alternating(10)
    assert compact.compact(compact.ContextState(), msgs, trigger="manual") is True  # auto-scoped hook must not block /compact


def test_precompact_payload_and_continue_false_precedence(tmp_path, monkeypatch):
    from minicc import context_management as compact

    seen = tmp_path / "pre.txt"
    cmd = (
        f"{sys.executable} -c \"import sys,json,pathlib; "
        f"d=json.load(sys.stdin); "
        f"pathlib.Path(r'{seen}').write_text(d['trigger']+'|'+d['custom_instructions']); "
        "print('{\\\"decision\\\":\\\"block\\\",\\\"continue\\\":false,"
        "\\\"stopReason\\\":\\\"halt\\\"}')\""
    )
    _use({"PreCompact": [_group(cmd, matcher="manual")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        summary,
        "_summarize",
        lambda *a, **k: pytest.fail("summary ran after continue:false"),
    )

    with pytest.raises(hooks.HookStop, match="halt"):
        compact.compact(
            compact.ContextState(),
            _alternating(10),
            trigger="manual",
            focus="keep tests",
        )
    assert seen.read_text() == "manual|keep tests"


# ─── wiring: SessionStart / SessionEnd in the CLI ───────────────────────────

def test_session_start_context_appended(monkeypatch):
    from minicc import cli

    cmd = """echo '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"branch is main"}}'"""
    _use({"SessionStart": [_group(cmd, matcher="startup")]}, monkeypatch=monkeypatch)
    ctx = cli._session_context_with_hooks("s1", "startup")
    assert "# Session context" in ctx          # env layer intact
    assert "branch is main" in ctx             # hook context appended


def test_session_start_matcher_filters_source(monkeypatch):
    from minicc import cli

    cmd = """echo '{"hookSpecificOutput":{"additionalContext":"CLEARED"}}'"""
    _use({"SessionStart": [_group(cmd, matcher="clear")]}, monkeypatch=monkeypatch)
    assert "CLEARED" not in cli._session_context_with_hooks("s1", "startup")
    assert "CLEARED" in cli._session_context_with_hooks("s1", "clear")


def test_successful_compaction_fires_compact_session_start(tmp_path, monkeypatch):
    from minicc import context_management as compact

    seen = tmp_path / "source.txt"
    cmd = (
        f"{sys.executable} -c \"import sys,json,pathlib; "
        f"d=json.load(sys.stdin); pathlib.Path(r'{seen}').write_text(d['source']); "
        "print('{\\\"hookSpecificOutput\\\":{\\\"additionalContext\\\":"
        "\\\"post-compact context\\\"}}')\""
    )
    _use({"SessionStart": [_group(cmd, matcher="compact")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(summary, "_summarize", lambda *a, **k: "SUMMARY")
    monkeypatch.setattr(manager.sessions, "log_compaction", lambda *a, **k: None)
    recorded = []
    monkeypatch.setattr(
        manager.sessions,
        "append_message",
        lambda session_id, message, **kwargs: recorded.append(message),
    )
    msgs = _alternating(10)

    assert compact.compact(
        compact.ContextState(),
        msgs,
        session_id="s1",
        model="claude-sonnet-4-6",
    ) is True
    assert seen.read_text() == "compact"
    assert msgs[-1] == {"role": "user", "content": "post-compact context"}
    assert recorded == [msgs[-1]]


def test_compact_session_context_persists_before_live_append(monkeypatch):
    from minicc import context_management as compact

    cmd = """echo '{"hookSpecificOutput":{"additionalContext":"must persist"}}'"""
    _use(
        {"SessionStart": [_group(cmd, matcher="compact")]},
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(summary, "_summarize", lambda *a, **k: "SUMMARY")
    monkeypatch.setattr(manager.sessions, "log_compaction", lambda *a, **k: None)
    monkeypatch.setattr(
        manager.sessions,
        "append_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    msgs = _alternating(10)
    ctx = compact.ContextState()

    with pytest.raises(OSError, match="disk full"):
        compact.compact(ctx, msgs, session_id="s1")

    assert all(message.get("content") != "must persist" for message in msgs)
    assert ctx.compactions == 1
    assert ctx.last_message_tokens == compact.estimate_tokens(msgs)


def test_session_end_is_informational_only(monkeypatch):
    from minicc import cli

    # exit 2 would block a blockable event; SessionEnd must ignore it (CC contract)
    _use({"SessionEnd": [_group("echo nope >&2; exit 2")]}, monkeypatch=monkeypatch)
    cli._fire_session_end("s1", "prompt_input_exit")  # must not raise / block anything


# ─── wiring: Stop gate in the agent loop ─────────────────────────────────────

class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    usage = None  # append_message(usage=None) skips the usage field

    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Text(text)]
        self.stop_reason = stop_reason


def _drive(monkeypatch, replies, session_id="s1"):
    """Run agent_loop against canned end_turn replies; returns (messages, calls)."""
    from minicc import query_engine as engine

    queue = list(replies)
    calls = []

    def fake_llm(messages, *a, **k):
        calls.append(len(messages))
        return _Resp(queue.pop(0) if queue else "done")

    monkeypatch.setattr(engine, "llm_response", fake_llm)
    monkeypatch.setattr(engine.sessions, "append_message", lambda *a, **k: None)
    messages = [{"role": "user", "content": "hi"}]
    engine.agent_loop(messages, session_id=session_id)
    return messages, calls


def test_pretool_continue_false_aborts_before_event_decision(monkeypatch):
    from types import SimpleNamespace
    from minicc import query_engine as engine

    cmd = (
        "echo '{\"continue\":false,\"stopReason\":\"halt tool\","
        "\"hookSpecificOutput\":{\"permissionDecision\":\"allow\"}}'"
    )
    _use({"PreToolUse": [_group(cmd, matcher="bash")]}, monkeypatch=monkeypatch)
    block = SimpleNamespace(name="bash", input={"command": "echo no"}, id="t1")

    with pytest.raises(hooks.HookStop, match="halt tool"):
        engine._run_tool(block, {"bash"}, "s1", "")


@pytest.mark.parametrize("blocks_so_far, expected", [(0, False), (2, True)])
def test_stop_payload_includes_stop_hook_active(
    tmp_path, monkeypatch, blocks_so_far, expected
):
    from minicc import query_engine as engine

    seen = tmp_path / f"active-{blocks_so_far}.txt"
    cmd = (
        f"{sys.executable} -c \"import sys,json,pathlib; "
        f"d=json.load(sys.stdin); pathlib.Path(r'{seen}').write_text("
        "str(d['stop_hook_active']).lower())\""
    )
    _use({"Stop": [_group(cmd)]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(engine.sessions, "append_message", lambda *a, **k: None)

    assert not engine._stop_gate(
        _Resp("done"),
        [{"role": "user", "content": "hi"}],
        "s1",
        "",
        blocks_so_far,
    )
    assert seen.read_text() == str(expected).lower()


def test_stop_block_feeds_reason_and_continues(tmp_path, monkeypatch):
    # Stateful hook: blocks the FIRST stop (creates a marker), allows the second —
    # also proves last_assistant_message reaches stdin (written to a file).
    mark, seen = tmp_path / "mark", tmp_path / "seen.txt"
    cmd = (
        f"{sys.executable} -c \""
        "import sys, json, pathlib\n"
        "d = json.load(sys.stdin)\n"
        f"pathlib.Path(r'{seen}').write_text(d['last_assistant_message'])\n"
        f"m = pathlib.Path(r'{mark}')\n"
        "if m.exists():\n"
        "    sys.exit(0)\n"
        "m.touch()\n"
        "print('run the tests first', file=sys.stderr)\n"
        "sys.exit(2)\""
    )
    _use({"Stop": [_group(cmd)]}, monkeypatch=monkeypatch)
    messages, calls = _drive(monkeypatch, ["first answer", "second answer"])
    assert len(calls) == 2                       # blocked once → one extra model call
    assert messages[2]["role"] == "user"
    assert "run the tests first" in messages[2]["content"]   # reason fed back
    assert seen.read_text() == "second answer"   # CC payload field, latest turn
    assert messages[-1]["content"][0].text == "second answer"


def test_stop_block_capped_after_8(monkeypatch):
    _use({"Stop": [_group("echo never >&2; exit 2")]}, monkeypatch=monkeypatch)
    messages, calls = _drive(monkeypatch, [])
    assert len(calls) == 9  # 8 blocks honored, 9th attempt overridden (CC cap)


def test_stop_skipped_for_subagents(monkeypatch):
    _use({"Stop": [_group("exit 2")]}, monkeypatch=monkeypatch)
    _messages, calls = _drive(monkeypatch, [], session_id=None)
    assert len(calls) == 1  # no Stop surface in sub-agent loops → ends immediately


def test_stop_additional_context_without_block_ends_turn(monkeypatch):
    cmd = (
        'echo \'{"hookSpecificOutput":'
        '{"hookEventName":"Stop","additionalContext":"deploy succeeded"}}\''
    )
    _use({"Stop": [_group(cmd)]}, monkeypatch=monkeypatch)
    messages, calls = _drive(monkeypatch, ["answer"])
    assert len(calls) == 1                        # turn ended (no block)
    assert messages[-1]["role"] == "user"         # context trails for next turn
    assert messages[-1]["content"] == "deploy succeeded"


def test_stop_continue_false_overrides_block(monkeypatch):
    cmd = 'echo \'{"decision":"block","reason":"more!","continue":false,"stopReason":"halted"}\''
    _use({"Stop": [_group(cmd)]}, monkeypatch=monkeypatch)
    _messages, calls = _drive(monkeypatch, ["answer"])
    assert len(calls) == 1  # continue:false wins over decision:block (CC precedence)
