"""Unit tests for the server-side web_search integration: registration shape,
no client handler, pause_turn continuation, usage counting, config opt-out,
and serialization of server blocks (encrypted_content must survive verbatim)."""

import os

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from minicc import config, sessions
from minicc.tools import TOOLS, TOOL_HANDLERS


# ─── registration: a server tool, not a client tool ──────────────────────────
def test_server_tool_entry_shape():
    entry = next(t for t in TOOLS if t["name"] == "web_search")
    assert entry["type"].startswith("web_search_")     # server tool type string
    assert "input_schema" not in entry                 # server tools carry no schema
    assert entry["max_uses"] == 8


def test_no_client_handler_and_not_for_subagents():
    from minicc import agents
    assert "web_search" not in TOOL_HANDLERS           # server-executed
    assert "web_search" not in agents.resolve("explore").tools


# ─── config opt-out ──────────────────────────────────────────────────────────
def test_web_search_enabled_default_and_optout(monkeypatch, tmp_path):
    home = tmp_path / "home"; proj = tmp_path / "proj"
    home.mkdir(); proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    assert config.web_search_enabled() is True         # default on
    (proj / ".minicc").mkdir()
    (proj / ".minicc" / "settings.json").write_text('{"web_search": false}')
    assert config.web_search_enabled() is False        # project opt-out


# ─── pause_turn: resend the paused assistant message unchanged ───────────────
def test_agent_loop_continues_on_pause_turn(monkeypatch):
    from minicc import query_engine as engine

    calls = {"n": 0}

    class _Blk:
        type, text = "text", "searching…"

    class _Resp:
        def __init__(self, stop):
            self.stop_reason = stop
            self.content = [_Blk()]

    def fake_llm(messages, system=None, stream=True, tools=None, model=None, session_id=None, ctx=None):
        calls["n"] += 1
        return _Resp("pause_turn" if calls["n"] == 1 else "end_turn")

    monkeypatch.setattr(engine, "llm_response", fake_llm)
    msgs = [{"role": "user", "content": "search something"}]
    engine.agent_loop(msgs, stream=False)
    assert calls["n"] == 2                             # paused once, resumed, finished
    # both assistant messages are in history (the paused one resent unchanged)
    assert [m["role"] for m in msgs] == ["user", "assistant", "assistant"]


# ─── usage counting ──────────────────────────────────────────────────────────
def test_llm_response_counts_web_searches(monkeypatch):
    from minicc import llm

    class _STU:
        web_search_requests = 3

    class _Usage:
        input_tokens = 10
        output_tokens = 5
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        server_tool_use = _STU()

    class _T:
        type, text = "text", "done"

    class _Resp:
        content = [_T()]
        usage = _Usage()
        stop_reason = "end_turn"

    monkeypatch.setattr(llm, "_send_request", lambda params, stream: _Resp())
    from minicc import compact
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    before = llm.get_usage()["web_searches"]
    llm.llm_response([{"role": "user", "content": "hi"}], stream=False)
    assert llm.get_usage()["web_searches"] == before + 3


# ─── serialization: encrypted_content must survive verbatim ──────────────────
def test_server_blocks_serialize_verbatim():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "server_tool_use", "id": "srv1", "name": "web_search",
             "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "srv1",
             "content": [{"type": "web_search_result", "url": "https://x.test",
                          "title": "T", "encrypted_content": "OPAQUE-BYTES=="}]},
        ],
    }
    out = sessions._serialize_message(msg)
    assert out == msg   # dict blocks pass through untouched — nothing re-encoded