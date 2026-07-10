"""Tests for the event-hook mechanism (minicc/hooks.py).

These exercise the CC-faithful contract directly — matcher semantics, the exit-code
protocol (0=JSON stdout, 2=block, other=non-blocking), and each JSON decision field —
by running real command hooks, so the doc'd behavior and the code can't drift.
"""

import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import hooks


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
    d = hooks.run("UserPromptSubmit", user_prompt="hi")
    assert d.stop and d.stop_reason == "halt"


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
        'ctx = d["tool_name"] + "|" + d["hook_event_name"] + "|" + d["cwd"]\n'
        'print(json.dumps({"hookSpecificOutput": {"additionalContext": ctx}}))\n'
    )
    _use({"PreToolUse": [_group(f"{sys.executable} {script}")]}, monkeypatch=monkeypatch)
    d = hooks.run("PreToolUse", match_value="bash", tool_name="bash", tool_input={"command": "ls"})
    assert len(d.additional_context) == 1
    tool_name, event, cwd = d.additional_context[0].split("|")
    assert tool_name == "bash" and event == "PreToolUse" and cwd  # common fields present


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
    g.write_text(_json.dumps({"hooks": {"PreToolUse": [_group("echo g")]}}))
    p.write_text(_json.dumps({"hooks": {"PreToolUse": [_group("echo p")]}, "disableAllHooks": False}))
    monkeypatch.setattr(config, "_global", lambda: g)
    monkeypatch.setattr(config, "_project", lambda: p)

    events, disabled = config.load_hooks()
    assert not disabled
    assert len(events["PreToolUse"]) == 2  # concatenated, both fire


def test_load_hooks_disable_in_either_file(tmp_path, monkeypatch):
    import json as _json
    from minicc import config

    g = tmp_path / "global.json"
    p = tmp_path / "project.json"
    g.write_text(_json.dumps({}))
    p.write_text(_json.dumps({"disableAllHooks": True}))
    monkeypatch.setattr(config, "_global", lambda: g)
    monkeypatch.setattr(config, "_project", lambda: p)

    _events, disabled = config.load_hooks()
    assert disabled


# ─── wiring: PreCompact / PostCompact around llm._compact ───────────────────

def _alternating(n):
    """n messages of valid user/assistant alternation (odd indices = assistant),
    long enough for _find_cut_index to find a safe cut."""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


def test_precompact_block_stops_compaction_before_llm(monkeypatch):
    from minicc import llm

    _use({"PreCompact": [_group("echo 'not now' >&2; exit 2", matcher="manual")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        llm, "_summarize", lambda *a, **k: pytest.fail("summary LLM call ran despite block")
    )
    msgs = _alternating(10)
    before = list(msgs)
    assert llm.compact(msgs) is False
    assert msgs == before  # history untouched


def test_postcompact_fires_with_compact_reason(tmp_path, monkeypatch):
    from minicc import llm

    out = tmp_path / "post.txt"
    cmd = (
        f"{sys.executable} -c \"import sys,json,pathlib; "
        f"pathlib.Path(r'{out}').write_text(json.load(sys.stdin)['compact_reason'])\""
    )
    _use({"PostCompact": [_group(cmd, matcher="manual")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(llm, "_summarize", lambda *a, **k: "SUMMARY")
    msgs = _alternating(10)
    assert llm.compact(msgs) is True
    assert msgs[0]["role"] == "user" and "SUMMARY" in msgs[0]["content"]
    assert out.read_text() == "manual"  # CC payload field, matcher matched


def test_precompact_auto_matcher_skips_manual_compact(monkeypatch):
    from minicc import llm

    _use({"PreCompact": [_group("exit 2", matcher="auto")]}, monkeypatch=monkeypatch)
    monkeypatch.setattr(llm, "_summarize", lambda *a, **k: "SUMMARY")
    msgs = _alternating(10)
    assert llm.compact(msgs) is True  # auto-scoped hook must not block /compact


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


def test_session_end_is_informational_only(monkeypatch):
    from minicc import cli

    # exit 2 would block a blockable event; SessionEnd must ignore it (CC contract)
    _use({"SessionEnd": [_group("echo nope >&2; exit 2")]}, monkeypatch=monkeypatch)
    cli._fire_session_end("s1", "prompt_input_exit")  # must not raise / block anything
