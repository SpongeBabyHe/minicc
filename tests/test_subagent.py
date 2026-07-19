"""Sub-agent runs on a cheaper model (D8) — threaded per-call, never mutating
the global MODEL (so the parent's model/cache stay intact)."""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import types


def _blk(**k):
    return types.SimpleNamespace(**k)


def test_agent_loop_threads_model_to_llm_response(monkeypatch):
    from minicc import query_engine as engine

    captured = {}

    def fake_llm_response(messages, system=None, stream=True, tools=None, model=None, session_id=None):
        captured["model"] = model
        return _blk(stop_reason="end_turn", content=[_blk(type="text", text="done")])

    monkeypatch.setattr(engine, "llm_response", fake_llm_response)
    engine.agent_loop([{"role": "user", "content": "hi"}], model="cheap-x")
    assert captured["model"] == "cheap-x"


def test_explore_subagent_uses_cheaper_model_without_mutating_global(monkeypatch):
    from minicc import query_engine as engine, agents, llm
    from minicc.tools import agent as agent_tool

    calls = {}

    def fake_agent_loop(messages, **kwargs):
        calls.update(kwargs)
        messages.append({"role": "assistant", "content": [_blk(type="text", text="summary")]})

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


def test_llm_response_uses_model_override_without_mutating_global(monkeypatch):
    from minicc import llm

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
