"""Unit tests for L3/L4/L5 context management in minicc.llm.

These are deterministic and make NO real API calls — the Anthropic client's
`create` is monkeypatched. The focus is structural correctness:

- L4 _find_cut_index: cuts at an assistant boundary (works mid-turn)
- L4 _compact: produces an API-valid message list (no orphaned tool_result,
  valid role alternation, first message is user, tool_use/tool_result pairs
  intact)
- L4 _summarize: CC-style — shares the live prefix (same system + tools) and
  appends the instruction as a final user message
- L3 _evict_old_tool_result: keeps the recent N
- L5 thrashing guard: raises after MAX_COMPACT_ATTEMPTS instead of looping
"""

import os

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import llm


# ─── fakes ──────────────────────────────────────────────────────────────────
class FakeToolUse:
    """Mimics an SDK ToolUseBlock (object, NOT dict)."""
    def __init__(self, id, name="read_file", input=None):
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input or {"path": "x.py"}


class FakeText:
    """Mimics an SDK TextBlock."""
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeUsage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class FakeResponse:
    def __init__(self, text="## Goal\nsummary\n## Done\n- stuff"):
        self.content = [FakeText(text)]
        self.usage = FakeUsage()
        self.stop_reason = "end_turn"


def fake_create(*args, **kwargs):
    return FakeResponse()


# ─── message builders ────────────────────────────────────────────────────────
def user(text):
    return {"role": "user", "content": text}


def assistant_call(tid, name="read_file", inp=None):
    return {"role": "assistant", "content": [FakeToolUse(tid, name, inp)]}


def tool_result(tid, content="file body"):
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}],
    }


def single_turn(n_files):
    """One user query + n (assistant tool_use, user tool_result) pairs."""
    msgs = [user("read files one by one")]
    for i in range(n_files):
        msgs.append(assistant_call(f"t{i}"))
        msgs.append(tool_result(f"t{i}"))
    return msgs


# ─── the structural validator (the crux) ─────────────────────────────────────
def assert_api_valid(messages):
    """Assert messages satisfy the Anthropic API's structural rules."""
    assert messages, "messages must be non-empty"
    assert messages[0]["role"] == "user", "first message must be user"

    # roles must alternate
    for a, b in zip(messages, messages[1:]):
        assert a["role"] != b["role"], f"consecutive same role: {a['role']}"

    # every tool_result must reference a tool_use seen earlier (no orphans)
    seen = set()
    for m in messages:
        content = m["content"]
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "tool_use":
                    seen.add(blk["id"])
                elif blk.get("type") == "tool_result":
                    assert blk["tool_use_id"] in seen, (
                        f"orphaned tool_result: {blk['tool_use_id']}"
                    )
            else:  # SDK object
                if getattr(blk, "type", None) == "tool_use":
                    seen.add(blk.id)


# ─── L4: _find_cut_index ─────────────────────────────────────────────────────
def test_find_cut_index_single_turn_returns_assistant():
    """Regression: single long turn used to return None → thrash. Now it must
    find an assistant boundary so compaction works mid-turn."""
    msgs = single_turn(4)  # 9 messages, only msgs[0] is a plain-string user
    cut = llm._find_cut_index(msgs)
    assert cut is not None, "must find a cut point mid-turn"
    assert msgs[cut]["role"] == "assistant", "cut must land before an assistant"
    assert cut >= 2


def test_find_cut_index_too_short_returns_none():
    msgs = [user("hi"), assistant_call("t0"), tool_result("t0")]
    # target = max(1, 3 - KEEP); first assistant likely at index 1 → cut<2 → None
    assert llm._find_cut_index(msgs) is None


# ─── L4: _compact structural validity (mock the API) ─────────────────────────
def test_compact_produces_valid_structure(monkeypatch):
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = single_turn(5)  # 11 messages
    ok = llm._compact(msgs)
    assert ok is True
    assert_api_valid(msgs)               # ← no orphaned tool_result, alternation, etc.
    assert msgs[0]["role"] == "user"
    assert "summary" in msgs[0]["content"].lower()


def test_compact_keeps_recent_pairs_intact(monkeypatch):
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = single_turn(5)
    llm._compact(msgs)
    # the tail after the summary must start with an assistant message
    assert msgs[1]["role"] == "assistant"
    assert_api_valid(msgs)


# ─── L4: _summarize shares the live prefix (CC-style compaction) ─────────────
def test_summarize_shares_prefix_and_appends_instruction(monkeypatch):
    captured = {}

    def capture_create(*args, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(llm.client.messages, "create", capture_create)
    msgs = single_turn(3)
    out = llm._summarize(msgs)

    assert "summary" in out.lower()
    # same prefix as a live turn: real system blocks + the full tool set, so the
    # call reads cache instead of reprocessing the history.
    assert captured["tools"] is llm.TOOLS
    assert isinstance(captured["system"], list) and captured["system"]
    # the instruction rides as the FINAL user message after the whole history.
    sent = captured["messages"]
    assert len(sent) == len(msgs) + 1
    assert sent[-1]["role"] == "user"
    assert "## Goal" in sent[-1]["content"][-1]["text"]      # _COMPACT_PROMPT body


# ─── Prompt-caching breakpoint budget (CC-style grouping) ────────────────────
def test_tools_carry_no_cache_breakpoint():
    """Tools are cached via the system-prompt breakpoint (tools render first), so
    none of them carry their own cache_control — keeping us inside the 4/request
    budget. See tools/__init__.py."""
    from minicc.tools import TOOLS, READ_ONLY_TOOLS

    assert all("cache_control" not in t for t in TOOLS)
    assert all("cache_control" not in t for t in READ_ONLY_TOOLS)


def test_request_stays_within_four_breakpoints(monkeypatch):
    """system (+ project context) + the rolling conversation marker must total
    <= 4 cache breakpoints, the API's hard limit."""
    llm.set_project_context("# Project context\nstuff")
    try:
        system_blocks = llm._build_system_block()
        sys_bps = sum(1 for b in system_blocks if "cache_control" in b)
        convo_bps = 1  # _cacheable marks the last message
        assert sys_bps + convo_bps <= 4
    finally:
        llm.set_project_context("")


# ─── L3: eviction keeps recent N ─────────────────────────────────────────────
def test_evict_keeps_recent(monkeypatch):
    monkeypatch.setattr(llm, "RECENT_TOOL_RESULTS_KEEP", 2)
    msgs = single_turn(6)  # 6 tool_results
    evicted = llm._evict_old_tool_result(msgs)
    assert evicted == 4  # 6 total - 2 kept
    # count non-evicted tool_results remaining
    live = sum(
        1
        for m in msgs
        if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict)
        and b.get("type") == "tool_result"
        and b["content"] != llm.EVICTED_MARKER
    )
    assert live == 2


# ─── L5: thrashing guard ─────────────────────────────────────────────────────
def test_thrash_guard_raises(monkeypatch):
    """When reduction can't get under budget, llm_response must raise after
    MAX_COMPACT_ATTEMPTS instead of looping forever."""
    monkeypatch.setattr(llm, "TOKEN_BUDGET", 1)          # everything is "over"
    monkeypatch.setattr(llm, "_evict_old_tool_result", lambda m: 0)
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: False)  # reduction impossible (now takes model=)
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)

    msgs = [user("x" * 100)]
    with pytest.raises(RuntimeError, match="thrashing"):
        for _ in range(llm.MAX_COMPACT_ATTEMPTS + 1):
            llm.llm_response(msgs, stream=False)


def test_no_thrash_when_under_budget(monkeypatch):
    """Under budget → no eviction/compaction, counter stays 0, no raise."""
    monkeypatch.setattr(llm, "TOKEN_BUDGET", 10_000_000)
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = [user("hi")]
    resp = llm.llm_response(msgs, stream=False)
    assert resp is not None
    assert llm._compact_attempts == 0


# ─── L1: conversation-history caching ────────────────────────────────────────
def test_cacheable_string_last_gets_breakpoint():
    msgs = [user("hello")]
    out = llm._cacheable(msgs)
    assert out[-1]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]
    assert msgs[0]["content"] == "hello"                     # input untouched


def test_cacheable_list_last_block_gets_breakpoint():
    msgs = [tool_result("t1", "body")]
    out = llm._cacheable(msgs)
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in msgs[0]["content"][0]      # input untouched


def test_cacheable_only_last_message_is_marked():
    msgs = [user("q1"), assistant_call("t1"), tool_result("t1"), user("q2")]
    out = llm._cacheable(msgs)
    assert "cache_control" not in out[0]["content"][0]       # q1 normalized, not marked
    assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}  # q2 marked
    assert out[1] is msgs[1]                                 # assistant passed through


def test_cacheable_does_not_mutate_input():
    msgs = [user("q1"), tool_result("t1")]
    llm._cacheable(msgs)
    assert msgs[0]["content"] == "q1"                        # string untouched
    assert "cache_control" not in msgs[1]["content"][0]      # tool_result untouched


def test_llm_response_caches_the_history(monkeypatch):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(llm.client.messages, "create", capture)
    llm.llm_response([user("hi")], stream=False)
    assert captured["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
