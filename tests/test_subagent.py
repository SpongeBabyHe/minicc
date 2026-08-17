"""Sub-agent runs on a cheaper model (D8) — threaded per-call, never mutating
the global MODEL (so the parent's model/cache stay intact)."""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import types

import pytest
from anthropic import Anthropic
from minicc import hooks


def _blk(**k):
    return types.SimpleNamespace(**k)


def test_agent_loop_threads_model_to_llm_response(monkeypatch):
    from minicc import query_engine as engine

    captured = {}

    def fake_llm_response(messages, system=None, stream=True, tools=None, model=None, session_id=None, ctx=None):
        captured["model"] = model
        return _blk(stop_reason="end_turn", content=[_blk(type="text", text="done")])

    monkeypatch.setattr(engine, "llm_response", fake_llm_response)
    outcome = engine.agent_loop(
        [{"role": "user", "content": "hi"}], model="cheap-x"
    )
    assert captured["model"] == "cheap-x"
    assert outcome == engine.TurnOutcome(
        status=engine.TurnStatus.COMPLETED,
        reason="end_turn",
        output_text="done",
        model_turns=1,
    )


def test_explore_subagent_uses_cheaper_model_without_mutating_global(monkeypatch):
    from minicc import query_engine as engine, agents, llm
    from minicc.tools import agent as agent_tool

    calls = {}

    def fake_agent_loop(messages, **kwargs):
        calls.update(kwargs)
        messages.append({"role": "assistant", "content": [_blk(type="text", text="summary")]})
        return engine.TurnOutcome(
            engine.TurnStatus.COMPLETED, "end_turn", "summary", 1
        )

    monkeypatch.setattr(engine, "agent_loop", fake_agent_loop)  # task imports it lazily
    before = llm.get_model()

    out = agent_tool.agent("explore the thing", subagent_type="explore")

    assert calls["model"] == agents.EXPLORE_MODEL      # explore runs on the cheap tier
    assert "haiku" in agents.EXPLORE_MODEL
    assert out == "summary"
    assert llm.get_model() == before                   # parent model untouched


def test_general_purpose_inherits_model_and_all_tools_minus_task(monkeypatch):
    from minicc import query_engine as engine
    from minicc.tools import agent as agent_tool

    calls = {}

    def fake_agent_loop(messages, **kwargs):
        calls.update(kwargs)
        messages.append({"role": "assistant", "content": [_blk(type="text", text="done")]})
        return engine.TurnOutcome(
            engine.TurnStatus.COMPLETED, "end_turn", "done", 1
        )

    monkeypatch.setattr(engine, "agent_loop", fake_agent_loop)
    agent_tool.agent("do the work")  # default subagent_type = general-purpose

    assert calls["model"] is None  # None = inherit the session model (CC parity)
    names = {t["name"] for t in calls["tools"]}
    assert "agent" not in names            # no nested sub-agents (D6)
    assert {"write_file", "bash", "read_file"} <= names  # full tool set otherwise


def test_unknown_subagent_type_is_a_value_not_a_crash():
    from minicc.tools import agent as agent_tool
    out = agent_tool.agent("x", subagent_type="nope")
    assert out.startswith("Error: unknown subagent_type 'nope'")


@pytest.mark.parametrize(
    "status, reason",
    [
        ("INCOMPLETE", "max_tokens"),
        ("LIMIT_REACHED", "max_turns"),
        ("REFUSED", "refusal"),
        ("STOPPED", "stop_hook"),
    ],
)
def test_subagent_noncompleted_result_is_never_a_bare_summary(
    monkeypatch, status, reason
):
    from minicc import query_engine as engine, ux
    from minicc.tools import agent as agent_tool

    said = []

    def fake_agent_loop(messages, **kwargs):
        return engine.TurnOutcome(
            getattr(engine.TurnStatus, status),
            reason,
            "partial findings",
            2,
        )

    monkeypatch.setattr(engine, "agent_loop", fake_agent_loop)
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))

    out = agent_tool.agent("investigate")

    assert out.startswith(
        f"Error: sub-agent ended {getattr(engine.TurnStatus, status).value}"
    )
    assert "Partial output — not a completed result:\npartial findings" in out
    assert out != "partial findings"
    assert not any(" done]" in line for line in said)


def test_subagent_noncompleted_without_partial_still_reports_status(monkeypatch):
    from minicc import query_engine as engine, ux
    from minicc.tools import agent as agent_tool

    said = []
    monkeypatch.setattr(
        engine,
        "agent_loop",
        lambda *a, **k: engine.TurnOutcome(
            engine.TurnStatus.REFUSED, "refusal", "", 1
        ),
    )
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))

    out = agent_tool.agent("investigate")

    assert out.startswith("Error: sub-agent ended refused (refusal)")
    assert "Partial output" not in out
    assert not any(" done]" in line for line in said)


def test_subagent_completed_without_summary_is_not_reported_as_done(monkeypatch):
    from minicc import query_engine as engine, ux
    from minicc.tools import agent as agent_tool

    said = []
    monkeypatch.setattr(
        engine,
        "agent_loop",
        lambda *a, **k: engine.TurnOutcome(
            engine.TurnStatus.COMPLETED, "end_turn", "", 1
        ),
    )
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))

    out = agent_tool.agent("investigate")

    assert out == "Error: sub-agent completed without a final summary."
    assert any("ended without a final summary" in line for line in said)
    assert not any(" done]" in line for line in said)


def test_llm_response_uses_model_override_without_mutating_global(monkeypatch):
    from minicc import llm
    monkeypatch.setattr(llm, "client", Anthropic(api_key="test-key"))

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        usage = _blk(input_tokens=1, output_tokens=1,
                     cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return _blk(content=[], stop_reason="end_turn", usage=usage)

    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    before = llm.get_model()

    llm.llm_response([{"role": "user", "content": "hi"}], stream=False, model="override-x")

    assert captured["model"] == "override-x"           # override used in the API params
    assert llm.get_model() == before                   # global MODEL not mutated


# ─── loop guards: turn limit, Stop-hook chain, tool availability ─────────────

def test_max_turns_exhaustion_is_announced(monkeypatch):
    """A sub-agent cut off at its turn limit must SAY so: it may have emitted
    text last turn, and that output must remain explicitly partial."""
    from minicc import query_engine as engine, ux

    said = []
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))
    calls = {"n": 0}

    def paused(*args, **kwargs):
        calls["n"] += 1
        return _blk(
            stop_reason="pause_turn",
            content=[_blk(type="text", text="partial findings")],
            usage=None,
        )

    monkeypatch.setattr(engine, "llm_response", paused)

    outcome = engine.agent_loop(
        [{"role": "user", "content": "go"}], max_turns=2
    )
    assert calls["n"] == 2
    assert outcome.status is engine.TurnStatus.LIMIT_REACHED
    assert outcome.reason == "max_turns"
    assert outcome.output_text == "partial findings"
    assert outcome.model_turns == 2
    assert any("2-turn limit" in s for s in said), said


def test_zero_turn_limit_returns_without_calling_model(monkeypatch):
    from minicc import query_engine as engine, ux

    monkeypatch.setattr(ux, "say", lambda *a, **k: None)
    monkeypatch.setattr(
        engine,
        "llm_response",
        lambda *a, **k: pytest.fail("max_turns=0 must not call the model"),
    )

    outcome = engine.agent_loop(
        [{"role": "user", "content": "go"}], max_turns=0
    )

    assert outcome == engine.TurnOutcome(
        engine.TurnStatus.LIMIT_REACHED,
        "max_turns",
        "",
        0,
    )


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: RuntimeError("API failed"),
        lambda: KeyboardInterrupt(),
        lambda: hooks.HookStop("PreToolUse", "halted"),
    ],
)
def test_agent_loop_preserves_caller_owned_exception_boundary(
    monkeypatch, error_factory
):
    from minicc import query_engine as engine

    error = error_factory()

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(engine, "llm_response", fail)
    messages = [{"role": "user", "content": "go"}]

    with pytest.raises(type(error)):
        engine.agent_loop(messages)

    assert messages == [{"role": "user", "content": "go"}]


def test_stop_block_chain_resets_after_productive_round(monkeypatch):
    """CC's guard is 8 CONSECUTIVE Stop-hook blocks. A real tool round breaks
    the chain, so the counter must go back to 0 — otherwise a cumulative count
    silently retires the hook after 8 non-consecutive blocks."""
    from minicc import query_engine as engine, ux

    monkeypatch.setattr(ux, "say", lambda *a, **k: None)
    seen = []

    # alternate: terminal turn (blocked by the Stop hook) -> tool round -> ...
    script = ["end_turn", "tool_use"] * 6
    step = {"i": 0}

    def fake_llm(*a, **k):
        i = step["i"]; step["i"] += 1
        if i >= len(script):
            return _blk(stop_reason="end_turn", content=[], usage=None)
        if script[i] == "tool_use":
            return _blk(
                stop_reason="tool_use",
                content=[
                    _blk(
                        type="tool_use",
                        id=f"tool-{i}",
                        name="no_such_tool",
                        input={},
                    )
                ],
                usage=None,
            )
        return _blk(stop_reason="end_turn", content=[], usage=None)

    def fake_stop_gate(response, messages, session_id, indent, blocks_so_far):
        seen.append(blocks_so_far)
        action = (
            engine._StopAction.RETRY
            if step["i"] < len(script)
            else engine._StopAction.ACCEPT
        )
        return engine._StopDecision(action)

    monkeypatch.setattr(engine, "llm_response", fake_llm)
    monkeypatch.setattr(engine, "_stop_gate", fake_stop_gate)
    outcome = engine.agent_loop(
        [{"role": "user", "content": "go"}], max_turns=20
    )

    # every block is preceded by a productive round, so the chain never builds
    assert max(seen) == 0, seen
    assert outcome.status is engine.TurnStatus.COMPLETED


def test_unavailable_tool_is_distinguished_from_unknown(monkeypatch):
    """A real tool that just isn't advertised to this agent must not be reported
    as 'Unknown' — that misleads the model into retrying a name it can see
    elsewhere."""
    from minicc import query_engine as engine, ux

    monkeypatch.setattr(ux, "say", lambda *a, **k: None)
    call = _blk(type="tool_use", id="t1", name="bash", input={"command": "ls"})

    # bash exists as a handler, but this agent was advertised only read_file
    out = engine._run_tool(call, allowed={"read_file"}, session_id=None, indent="")
    assert "not available to this agent" in out
    assert "Unknown tool" not in out

    ghost = _blk(type="tool_use", id="t2", name="no_such_tool", input={})
    out2 = engine._run_tool(ghost, allowed={"read_file"}, session_id=None, indent="")
    assert "Unknown tool: no_such_tool" in out2
