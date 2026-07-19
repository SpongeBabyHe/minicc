"""Tests for the max_tokens-truncation contract (dogfood R5, layer 4).

The failure: a stream cut by the output-token cap ends with
stop_reason="max_tokens" while its content still carries a PARTIAL tool_use
(truncated, often empty input). The terminal branch used to record it as-is
— no exception, no rollback — and the next request 400'd with "tool_use ids
were found without tool_result blocks". Two defenses, both pinned here:
the live path discards the partial call; the replay path repairs old
transcripts that already carry one.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import json

from minicc import query_engine as engine, sessions


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text

    def model_dump(self, exclude_none=True):  # sessions serializes like SDK blocks
        return {"type": "text", "text": self.text}


class _ToolUse:
    type = "tool_use"

    def __init__(self, id="t1", name="task_create", input=None):
        self.id, self.name, self.input = id, name, input or {}


class _Resp:
    usage = None

    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


def test_max_tokens_partial_tool_use_discarded(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # sessions write under cwd/.minicc
    monkeypatch.setattr(
        engine, "llm_response",
        lambda *a, **k: _Resp([_Text("half-done"), _ToolUse()], "max_tokens"),
    )
    messages = [{"role": "user", "content": "go"}]
    engine.agent_loop(messages, session_id="trunc1")

    def _btype(b):
        return b.get("type") if isinstance(b, dict) else getattr(b, "type", None)

    tail = messages[-1]
    assert tail["role"] == "assistant"
    assert all(_btype(b) != "tool_use" for b in tail["content"])
    # the surviving text block is kept
    assert any(getattr(b, "text", "") == "half-done" for b in tail["content"])
    # a resumed session must be API-valid too
    for m in sessions.load("trunc1"):
        if m["role"] == "assistant" and isinstance(m["content"], list):
            assert all(b.get("type") != "tool_use" for b in m["content"])


def test_all_tool_use_content_gets_placeholder(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        engine, "llm_response", lambda *a, **k: _Resp([_ToolUse()], "max_tokens")
    )
    messages = [{"role": "user", "content": "go"}]
    engine.agent_loop(messages, session_id=None)
    assert messages[-1]["content"] == [
        {"type": "text", "text": "[response truncated at the output-token limit]"}
    ]


def test_replay_repairs_dangling_tool_use(tmp_path, monkeypatch):
    """A pre-fix transcript (like R5's) resumes without a 400: the dangling
    tool_use gets a synthetic result inserted at load."""
    monkeypatch.chdir(tmp_path)
    sid = "poisoned"
    sessions.append_message(sid, {"role": "user", "content": "task"})
    sessions.append_message(sid, {"role": "assistant", "content": [
        {"type": "text", "text": "thinking..."},
        {"type": "tool_use", "id": "tu_dangling", "name": "task_create", "input": {}},
    ]})
    sessions.append_message(sid, {"role": "user", "content": "继续上面的任务"})

    loaded = sessions.load(sid)
    idx = next(i for i, m in enumerate(loaded)
               if m["role"] == "assistant" and isinstance(m["content"], list))
    nxt = loaded[idx + 1]
    assert nxt["role"] == "user"
    assert nxt["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu_dangling",
        "content": "[interrupted: no result was recorded]",
    }
    assert loaded[-1]["content"] == "继续上面的任务"  # the typed query survives, after the repair


def test_replay_merges_into_partial_results(tmp_path, monkeypatch):
    """Two tool_uses, only one answered: the synthetic result must be MERGED
    into the existing next message (all results must share one message)."""
    monkeypatch.chdir(tmp_path)
    sid = "partial"
    sessions.append_message(sid, {"role": "assistant", "content": [
        {"type": "tool_use", "id": "a", "name": "grep", "input": {}},
        {"type": "tool_use", "id": "b", "name": "glob", "input": {}},
    ]})
    sessions.append_message(sid, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": "ok"},
    ]})
    loaded = sessions.load(sid)
    assert len(loaded) == 2  # merged, not inserted
    ids = {b["tool_use_id"] for b in loaded[1]["content"]}
    assert ids == {"a", "b"}
