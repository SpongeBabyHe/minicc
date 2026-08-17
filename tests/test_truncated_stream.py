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

import pytest

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
        engine,
        "_stop_gate",
        lambda *a, **k: pytest.fail("truncation must not run the Stop gate"),
    )
    monkeypatch.setattr(
        engine, "llm_response",
        lambda *a, **k: _Resp([_Text("half-done"), _ToolUse()], "max_tokens"),
    )
    messages = [{"role": "user", "content": "go"}]
    outcome = engine.agent_loop(messages, session_id="trunc1")

    assert outcome.status is engine.TurnStatus.INCOMPLETE
    assert outcome.reason == "max_tokens"
    assert outcome.output_text == "half-done"

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
    outcome = engine.agent_loop(messages, session_id=None)
    assert outcome.status is engine.TurnStatus.INCOMPLETE
    assert outcome.reason == "max_tokens"
    assert messages[-1]["content"] == [
        {"type": "text", "text": "[response truncated at the output-token limit]"}
    ]
    assert outcome.output_text == "[response truncated at the output-token limit]"


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


# ─── stop_reason coverage: refusal + text-only max_tokens ────────────────────

def test_refusal_never_enters_history_or_transcript(monkeypatch, tmp_path):
    """A refusal is HTTP 200 with an EMPTY content array. An empty assistant
    message is invalid as next-turn input, so it must not reach history or the
    transcript — same poisoning class as a dangling tool_use."""
    import types
    from minicc import query_engine as engine, ux, sessions

    monkeypatch.chdir(tmp_path)
    said, recorded = [], []
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))
    monkeypatch.setattr(sessions, "append_message",
                        lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(
        engine,
        "_stop_gate",
        lambda *a, **k: pytest.fail("refusal must not run the Stop gate"),
    )
    monkeypatch.setattr(
        engine, "llm_response",
        lambda *a, **k: types.SimpleNamespace(
            stop_reason="refusal", content=[], usage=None),
    )

    msgs = [{"role": "user", "content": "something"}]
    outcome = engine.agent_loop(msgs, session_id="s1")

    assert msgs == [{"role": "user", "content": "something"}]  # rolled back
    assert recorded == []                                      # nothing persisted
    assert any("declined" in s for s in said), said
    assert outcome.status is engine.TurnStatus.REFUSED
    assert outcome.reason == "refusal"
    assert outcome.output_text == ""


@pytest.mark.parametrize("reason", ["max_tokens", "model_context_window_exceeded"])
def test_text_only_truncation_is_announced(monkeypatch, reason):
    """A text-only truncation keeps valid content, but ending silently hides a
    cut-off answer. Both max_tokens (output cap) and model_context_window_exceeded
    (generation ran into the window; 4.5+ models) must be announced."""
    import types
    from minicc import query_engine as engine, ux

    said = []
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))
    monkeypatch.setattr(
        engine, "llm_response",
        lambda *a, **k: types.SimpleNamespace(
            stop_reason=reason,
            content=[types.SimpleNamespace(type="text", text="half a sen")],
            usage=None),
    )

    outcome = engine.agent_loop(
        [{"role": "user", "content": "write an essay"}]
    )
    assert any("cut off" in s and reason in s for s in said), said
    assert outcome.status is engine.TurnStatus.INCOMPLETE
    assert outcome.reason == reason
    assert outcome.output_text == "half a sen"


@pytest.mark.parametrize(
    "provider_reason, outcome_reason",
    [("future_reason", "future_reason"), (None, "unknown_stop_reason")],
)
def test_unknown_stop_reason_is_not_treated_as_completed(
    monkeypatch, provider_reason, outcome_reason
):
    import types
    from minicc import query_engine as engine, ux

    said = []
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))
    monkeypatch.setattr(
        engine,
        "llm_response",
        lambda *a, **k: types.SimpleNamespace(
            stop_reason=provider_reason,
            content=[types.SimpleNamespace(type="text", text="possibly partial")],
            usage=None,
        ),
    )

    outcome = engine.agent_loop([{"role": "user", "content": "go"}])

    assert outcome.status is engine.TurnStatus.INCOMPLETE
    assert outcome.reason == outcome_reason
    assert outcome.output_text == "possibly partial"
    assert any(outcome_reason in line for line in said)


@pytest.mark.parametrize("reason", ["end_turn", "stop_sequence"])
def test_normal_stop_is_persisted_before_completion_gate(
    monkeypatch, tmp_path, reason
):
    import types
    from minicc import query_engine as engine

    monkeypatch.chdir(tmp_path)
    recorded = []
    monkeypatch.setattr(
        engine.sessions,
        "append_message",
        lambda _sid, message, **_kwargs: recorded.append(message),
    )

    def accept_after_persistence(response, *_args, **_kwargs):
        assert response.stop_reason == reason
        assert [message["role"] for message in recorded] == ["assistant"]
        return engine._StopDecision(engine._StopAction.ACCEPT)

    monkeypatch.setattr(engine, "_stop_gate", accept_after_persistence)
    monkeypatch.setattr(
        engine,
        "llm_response",
        lambda *a, **k: types.SimpleNamespace(
            stop_reason=reason,
            content=[types.SimpleNamespace(type="text", text="done")],
            usage=None,
        ),
    )

    outcome = engine.agent_loop(
        [{"role": "user", "content": "go"}], session_id="s1"
    )

    assert outcome == engine.TurnOutcome(
        engine.TurnStatus.COMPLETED,
        reason,
        "done",
        1,
    )


def test_outcome_text_supports_serialized_multiple_blocks(monkeypatch):
    import types
    from minicc import query_engine as engine

    monkeypatch.setattr(
        engine,
        "llm_response",
        lambda *a, **k: types.SimpleNamespace(
            stop_reason="end_turn",
            content=[
                {"type": "text", "text": " first "},
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "second"},
            ],
            usage=None,
        ),
    )

    outcome = engine.agent_loop([{"role": "user", "content": "go"}])

    assert outcome.status is engine.TurnStatus.COMPLETED
    assert outcome.output_text == "first \nsecond"


def test_tool_use_stop_without_tool_block_is_incomplete(monkeypatch):
    import types
    from minicc import query_engine as engine, ux

    said = []
    monkeypatch.setattr(ux, "say", lambda text, style="": said.append(str(text)))
    monkeypatch.setattr(
        engine,
        "llm_response",
        lambda *a, **k: types.SimpleNamespace(
            stop_reason="tool_use",
            content=[types.SimpleNamespace(type="text", text="no call supplied")],
            usage=None,
        ),
    )
    messages = [{"role": "user", "content": "go"}]

    outcome = engine.agent_loop(messages)

    assert outcome.status is engine.TurnStatus.INCOMPLETE
    assert outcome.reason == "tool_use_without_tool_block"
    assert outcome.output_text == "no call supplied"
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert any("no tool call" in line for line in said)


def test_tool_round_persistence_waits_for_every_result(monkeypatch, tmp_path):
    """A tool round is recorded only after every result exists, then in the
    provider-required assistant → user(tool_result) order."""
    import types
    from minicc import query_engine as engine

    monkeypatch.chdir(tmp_path)
    tool_blocks = [
        types.SimpleNamespace(
            type="tool_use", id=tool_id, name="fake", input={"id": tool_id}
        )
        for tool_id in ("t1", "t2")
    ]
    responses = iter(
        [
            types.SimpleNamespace(
                stop_reason="tool_use", content=tool_blocks, usage=None
            ),
            types.SimpleNamespace(
                stop_reason="end_turn",
                content=[types.SimpleNamespace(type="text", text="done")],
                usage=None,
            ),
        ]
    )
    recorded = []
    executed = []

    monkeypatch.setattr(engine, "llm_response", lambda *a, **k: next(responses))
    monkeypatch.setattr(
        engine.sessions,
        "append_message",
        lambda _sid, message, **_kwargs: recorded.append(message),
    )
    monkeypatch.setattr(
        engine,
        "_stop_gate",
        lambda *a, **k: engine._StopDecision(engine._StopAction.ACCEPT),
    )

    def run_tool(block, *_args, **_kwargs):
        assert recorded == []
        executed.append(block.id)
        return f"result-{block.id}"

    monkeypatch.setattr(engine, "_run_tool", run_tool)

    outcome = engine.agent_loop(
        [{"role": "user", "content": "go"}],
        tools=[{"name": "fake", "input_schema": {}}],
        session_id="s1",
    )

    assert outcome.completed
    assert executed == ["t1", "t2"]
    assert [message["role"] for message in recorded] == [
        "assistant",
        "user",
        "assistant",
    ]
    assert [
        block["tool_use_id"] for block in recorded[1]["content"]
    ] == ["t1", "t2"]
