"""Sub-agent runs on a cheaper model (D8) — threaded per-call, never mutating
the global MODEL (so the parent's model/cache stay intact)."""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import types


def _blk(**k):
    return types.SimpleNamespace(**k)


def test_agent_loop_threads_model_to_llm_response(monkeypatch):
    from minicc import agent

    captured = {}

    def fake_llm_response(messages, system=None, stream=True, tools=None, model=None):
        captured["model"] = model
        return _blk(stop_reason="end_turn", content=[_blk(type="text", text="done")])

    monkeypatch.setattr(agent, "llm_response", fake_llm_response)
    agent.agent_loop([{"role": "user", "content": "hi"}], model="cheap-x")
    assert captured["model"] == "cheap-x"


def test_task_uses_cheaper_model_without_mutating_global(monkeypatch):
    from minicc import agent, llm
    from minicc.tools import task as task_mod

    calls = {}

    def fake_agent_loop(messages, **kwargs):
        calls.update(kwargs)
        messages.append({"role": "assistant", "content": [_blk(type="text", text="summary")]})

    monkeypatch.setattr(agent, "agent_loop", fake_agent_loop)  # task imports it lazily
    before = llm.get_model()

    out = task_mod.task("explore the thing")

    assert calls["model"] == task_mod.SUBAGENT_MODEL
    assert "haiku" in task_mod.SUBAGENT_MODEL          # it's the cheap tier
    assert out == "summary"
    assert llm.get_model() == before                  # parent model untouched


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
