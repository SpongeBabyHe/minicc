"""Per-session WebSearch and subagent runaway-limit tests."""

import os
from types import SimpleNamespace

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from minicc import query_engine, session_limits, sessions


def _usage(web_searches: int = 0):
    return SimpleNamespace(
        input_tokens=1,
        output_tokens=1,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=web_searches),
    )


class _Block:
    def __init__(self, block_type: str, **fields):
        self.type = block_type
        self.__dict__.update(fields)

    def model_dump(self, exclude_none=True):
        return {
            key: value
            for key, value in self.__dict__.items()
            if not exclude_none or value is not None
        }


def test_limits_restore_consumed_counts_and_clamp_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(session_limits.WEB_SEARCH_ENV, "200")
    monkeypatch.setenv(session_limits.SUBAGENT_ENV, "1")
    sessions.record_counter("s", session_limits.WEB_SEARCH_COUNTER, 198)
    sessions.record_counter("s", session_limits.SUBAGENT_COUNTER)

    limits = session_limits.SessionLimits.load("s")
    tools = limits.tools_for_request(
        [
            {"name": "agent", "input_schema": {}},
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
            {"name": "read_file", "input_schema": {}},
        ]
    )

    assert {tool["name"] for tool in tools} == {"web_search", "read_file"}
    assert next(tool for tool in tools if tool["name"] == "web_search")[
        "max_uses"
    ] == 2


def test_invalid_environment_overrides_fall_back(monkeypatch):
    monkeypatch.setenv(session_limits.WEB_SEARCH_ENV, "not-an-int")
    monkeypatch.setenv(session_limits.SUBAGENT_ENV, "-1")

    limits = session_limits.SessionLimits.load(None)

    assert limits.max_web_searches == session_limits.DEFAULT_MAX_WEB_SEARCHES
    assert limits.max_subagents == session_limits.DEFAULT_MAX_SUBAGENTS


def test_zero_environment_override_disables_tools(monkeypatch):
    monkeypatch.setenv(session_limits.WEB_SEARCH_ENV, "0")
    monkeypatch.setenv(session_limits.SUBAGENT_ENV, "0")
    limits = session_limits.SessionLimits.load(None)

    assert limits.tools_for_request(
        [
            {"name": "agent"},
            {"name": "web_search", "max_uses": 8},
            {"name": "read_file"},
        ]
    ) == [{"name": "read_file"}]


def test_web_search_usage_is_persisted_across_loop_instances(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sessions.append_message("s", {"role": "user", "content": "search"})
    limits = session_limits.SessionLimits.load("s")
    response = SimpleNamespace(usage=_usage(web_searches=3))

    limits.record_response(response)
    restored = session_limits.SessionLimits.load("s")

    assert restored.web_searches == 3
    assert sessions.counter_totals("s") == {
        session_limits.WEB_SEARCH_COUNTER: 3
    }


def test_agent_loop_enforces_subagent_cap_within_one_response(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(session_limits.SUBAGENT_ENV, "1")
    sessions.append_message("s", {"role": "user", "content": "delegate"})
    calls = {"handler": 0, "requests": []}

    def handler(**tool_input):
        calls["handler"] += 1
        return f"handled {tool_input['prompt']}"

    monkeypatch.setitem(query_engine.TOOL_HANDLERS, "agent", handler)
    responses = iter(
        [
            SimpleNamespace(
                stop_reason="tool_use",
                usage=_usage(),
                content=[
                    _Block(
                        "tool_use",
                        id="a1",
                        name="agent",
                        input={"prompt": "first"},
                    ),
                    _Block(
                        "tool_use",
                        id="a2",
                        name="agent",
                        input={"prompt": "second"},
                    ),
                ],
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                usage=_usage(),
                content=[_Block("text", text="done")],
            ),
        ]
    )

    def fake_llm(messages, system=None, stream=True, tools=None, **kwargs):
        calls["requests"].append(tools)
        return next(responses)

    monkeypatch.setattr(query_engine, "llm_response", fake_llm)
    messages = [{"role": "user", "content": "delegate"}]
    query_engine.agent_loop(
        messages,
        tools=[{"name": "agent", "input_schema": {}}],
        session_id="s",
        stream=False,
    )

    assert calls["handler"] == 1
    assert [tool["name"] for tool in calls["requests"][0]] == ["agent"]
    assert calls["requests"][1] == []
    assert "session subagent limit reached" in messages[2]["content"][1]["content"]
    assert sessions.counter_totals("s") == {
        session_limits.SUBAGENT_COUNTER: 1
    }
