"""Unit tests for L3/L4/L5 context management in minicc.llm.

These are deterministic and make NO real API calls — the Anthropic client's
`create` is monkeypatched. The focus is structural correctness:

- L4 _find_cut_index: cuts at an assistant boundary (works mid-turn)
- L4 _compact: produces an API-valid message list (no orphaned tool_result,
  valid role alternation, first message is user, tool_use/tool_result pairs
  intact)
- L4 _summarize: CC-style — shares the live prefix (same system + tools) and
  appends the instruction as a final user message
- L3 _evict_old_tool_result: keeps the recent N; the clear_at_least guard skips
  an eviction that would free too little (don't break the cache for a small gain)
- two-band sizing: below CLEAR_TRIGGER nothing fires; between it and the budget L3
  evicts; over budget L4 compacts and skips eviction (keeps the summary warm)
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
    assert "## Current Work" in sent[-1]["content"][-1]["text"]  # _COMPACT_PROMPT body


# ─── L4: never destroy history on an empty summary (regression) ──────────────
class _ToolOnlyResponse:
    """A response with a tool_use block and NO text — what the model may return
    when tools are in scope and it tool-calls instead of summarizing."""
    content = [FakeToolUse("t0")]
    usage = FakeUsage()
    stop_reason = "tool_use"


def test_summarize_returns_none_when_no_text(monkeypatch):
    monkeypatch.setattr(llm.client.messages, "create", lambda *a, **k: _ToolOnlyResponse())
    assert llm._summarize(single_turn(3)) is None     # never a fake summary string


def test_compact_refuses_to_wipe_history_on_empty_summary(monkeypatch):
    monkeypatch.setattr(llm.client.messages, "create", lambda *a, **k: _ToolOnlyResponse())
    msgs = single_turn(5)
    before = list(msgs)
    assert llm._compact(msgs) is False
    assert msgs == before                              # history left intact


def test_subagent_compaction_uses_its_own_prefix(monkeypatch):
    """A sub-agent compacts under ITS system + tools, not the main agent's —
    else the prefix mismatches (cache miss + wrong summary context)."""
    captured = {}

    def capture_create(*args, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(llm.client.messages, "create", capture_create)
    sub_tools = [{"name": "read_file"}]
    llm._compact(single_turn(5), system="SUBAGENT PROMPT", tools=sub_tools)
    assert captured["tools"] is sub_tools
    assert captured["system"][0]["text"] == "SUBAGENT PROMPT"


# ─── Prompt-caching breakpoint budget (CC-style grouping) ────────────────────
def test_tools_carry_no_cache_breakpoint():
    """Tools are cached via the system-prompt breakpoint (tools render first), so
    none of them carry their own cache_control — keeping us inside the 4/request
    budget. See tools/__init__.py."""
    from minicc.tools import TOOLS, READ_ONLY_TOOLS

    assert all("cache_control" not in t for t in TOOLS)
    assert all("cache_control" not in t for t in READ_ONLY_TOOLS)


def test_request_stays_within_four_breakpoints(monkeypatch):
    """system + project + session + the rolling conversation marker = exactly the
    API's 4 cache breakpoints, and never more."""
    llm.set_project_context("# Project context\nstuff")
    llm.set_session_context("# Session context\n- cwd: /x")
    try:
        system_blocks = llm._build_system_block()
        sys_bps = sum(1 for b in system_blocks if "cache_control" in b)
        convo_bps = 1  # _cacheable marks the last message
        assert sys_bps == 3                 # system + project + session
        assert sys_bps + convo_bps == 4     # the full budget, not over it
    finally:
        llm.set_project_context("")
        llm.set_session_context("")


def test_session_context_is_volatile_last(monkeypatch):
    """Session context is the LAST system block (so a change never busts the static
    prefix above it) and carries its own cache breakpoint; static SYSTEM stays first."""
    llm.set_project_context("# Project\nx")
    llm.set_session_context("# Session context\n- Date: 2026-07-01")
    try:
        blocks = llm._build_system_block()
        assert blocks[0]["text"] == llm.SYSTEM                     # static first
        assert blocks[-1]["text"].startswith("# Session context")  # volatile last
        assert "cache_control" in blocks[-1]
    finally:
        llm.set_project_context("")
        llm.set_session_context("")


def test_no_session_block_when_unset():
    """Unset (e.g. sub-agents / tests) → no session block appears."""
    llm.set_project_context("")
    llm.set_session_context("")
    blocks = llm._build_system_block()
    assert len(blocks) == 1 and blocks[0]["text"] == llm.SYSTEM


def test_build_session_context_has_env(monkeypatch, tmp_path):
    """build_session_context reports cwd/platform/date; git line is skipped cleanly
    in a non-repo directory."""
    from minicc.prompts.system import build_session_context
    monkeypatch.chdir(tmp_path)
    ctx = build_session_context()
    assert ctx.startswith("# Session context")
    assert str(tmp_path) in ctx
    assert "Date:" in ctx and "Platform:" in ctx


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


# ─── Window-relative compaction budget (CC-style trigger) ────────────────────
def test_effective_budget_is_window_minus_buffer():
    # CC-aligned: window - 13K, no sub-window clamp (the old 350K ceiling was dropped).
    b = llm.COMPACT_BUFFER_TOKENS
    assert llm._effective_budget("claude-haiku-4-5") == 200_000 - b
    assert llm._effective_budget("claude-haiku-4-5-20251001") == 200_000 - b   # date suffix ok
    assert llm._effective_budget("claude-sonnet-4-6") == 1_000_000 - b         # 1M, no clamp
    assert llm._effective_budget("who-knows") == llm._DEFAULT_WINDOW - b


def test_over_budget_compacts_and_skips_eviction(monkeypatch):
    """Upper band: over the compaction budget → L4 compacts directly, and L3
    eviction is deliberately skipped that turn so the summary call reads a warm
    cache (no in-place rewrite right before compaction)."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 1)   # everything over
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    calls = {"compact": 0, "evict": 0}
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: calls.update(compact=calls["compact"] + 1) or True)
    monkeypatch.setattr(llm, "_evict_old_tool_result", lambda m, min_free=0: calls.update(evict=calls["evict"] + 1) or 0)

    llm.llm_response([user("x" * 100)], stream=False)
    assert calls["compact"] == 1
    assert calls["evict"] == 0


def test_midband_evicts_with_clear_at_least_guard(monkeypatch):
    """Lower band: CLEAR_TRIGGER < size <= budget → L3 evicts incrementally
    (passing the CLEAR_AT_LEAST guard as min_free) and L4 does NOT run."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 200_000)
    monkeypatch.setattr(llm, "_context_size", lambda m: 150_000)   # between trigger and budget
    monkeypatch.setattr(llm, "CLEAR_TRIGGER", 100_000)
    monkeypatch.setattr(llm, "CLEAR_AT_LEAST", 5_000)
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    calls = {"compact": 0, "evict": 0, "min_free": None}
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: calls.update(compact=calls["compact"] + 1) or True)

    def fake_evict(m, min_free=0):
        calls["evict"] += 1
        calls["min_free"] = min_free
        return 3
    monkeypatch.setattr(llm, "_evict_old_tool_result", fake_evict)

    llm.llm_response([user("hello")], stream=False)
    assert calls["compact"] == 0           # under budget → no compaction
    assert calls["evict"] == 1             # but over CLEAR_TRIGGER → evict
    assert calls["min_free"] == 5_000      # guarded by clear_at_least


def test_below_clear_trigger_does_not_evict(monkeypatch):
    """Below CLEAR_TRIGGER → neither L3 nor L4 fires (the cheap common case)."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 200_000)
    monkeypatch.setattr(llm, "_context_size", lambda m: 50_000)    # below the trigger
    monkeypatch.setattr(llm, "CLEAR_TRIGGER", 100_000)
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    calls = {"compact": 0, "evict": 0}
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: calls.update(compact=calls["compact"] + 1) or True)
    monkeypatch.setattr(llm, "_evict_old_tool_result", lambda m, min_free=0: calls.update(evict=calls["evict"] + 1) or 0)

    llm.llm_response([user("hi")], stream=False)
    assert calls["compact"] == 0
    assert calls["evict"] == 0


def test_evict_skips_when_below_min_free(monkeypatch):
    """The clear_at_least guard: if eviction would free fewer than min_free
    tokens, _evict_old_tool_result skips entirely (returns 0, mutates nothing) —
    don't break the prompt cache for a gain too small to be worth the rewrite."""
    monkeypatch.setattr(llm, "RECENT_TOOL_RESULTS_KEEP", 2)
    msgs = single_turn(6)            # 4 evictable tool_results, ~9 chars each ≈ 9 tokens freed
    before = [m["content"] for m in msgs]

    assert llm._evict_old_tool_result(msgs, min_free=1_000) == 0   # 9 < 1000 → skip
    assert [m["content"] for m in msgs] == before                 # untouched

    evicted = llm._evict_old_tool_result(msgs, min_free=1)         # 9 >= 1 → proceed
    assert evicted == 4


# ─── L5: thrashing guard ─────────────────────────────────────────────────────
def test_thrash_guard_raises(monkeypatch):
    """When compaction can't get under budget, llm_response must raise after
    MAX_COMPACT_ATTEMPTS instead of looping forever."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 1)   # everything over
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: False)      # reduction impossible
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)

    msgs = [user("x" * 100)]
    with pytest.raises(RuntimeError, match="thrashing"):
        for _ in range(llm.MAX_COMPACT_ATTEMPTS + 1):
            llm.llm_response(msgs, stream=False)


def test_no_thrash_when_under_budget(monkeypatch):
    """Under budget → no compaction, counter stays 0, no raise."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = [user("hi")]
    resp = llm.llm_response(msgs, stream=False)
    assert resp is not None
    assert llm._compact_attempts == 0


# ─── Reactive compaction on a 413 (request-too-large) fallback ───────────────
class _Fake413(llm.APIStatusError):
    """A 413 without the SDK's heavy __init__ — only .status_code is read."""
    def __init__(self):
        self.status_code = 413


def test_reactive_compact_on_413(monkeypatch):
    """A 413 forces one compaction and retries the send once."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 10_000_000)  # no pre-compact
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    calls = {"send": 0, "compact": 0}

    def flaky_send(params, stream):
        calls["send"] += 1
        if calls["send"] == 1:
            raise _Fake413()
        return FakeResponse()

    monkeypatch.setattr(llm, "_send_request", flaky_send)
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: calls.update(compact=calls["compact"] + 1) or True)

    resp = llm.llm_response([user("hi")], stream=False)
    assert resp is not None
    assert calls["send"] == 2      # failed once, retried once
    assert calls["compact"] == 1   # one forced compaction


def test_reactive_compact_gives_up_when_uncompactable(monkeypatch):
    """If compaction can't reduce (returns False), the 413 propagates."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: (_ for _ in ()).throw(_Fake413()))
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: False)
    with pytest.raises(llm.APIStatusError):
        llm.llm_response([user("hi")], stream=False)


def test_non_413_status_error_propagates(monkeypatch):
    """A non-413 API error is not treated as too-large; it propagates as-is."""
    class _Fake500(llm.APIStatusError):
        def __init__(self):
            self.status_code = 500

    monkeypatch.setattr(llm, "_effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: (_ for _ in ()).throw(_Fake500()))
    compacted = {"n": 0}
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: compacted.update(n=compacted["n"] + 1) or True)
    with pytest.raises(llm.APIStatusError):
        llm.llm_response([user("hi")], stream=False)


# ─── Reactive compaction on a 429 (rate-limit) fallback ──────────────────────
class _Fake429(llm.APIStatusError):
    """A 429 (rate limit); only .status_code is read."""
    def __init__(self):
        self.status_code = 429


def test_reactive_compact_on_429_large_context(monkeypatch):
    """A persistent 429 on a LARGE request → treat as over-ITPM (the PAIN.md case),
    compact to shrink it and retry once."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 10_000_000)  # no pre-compact
    monkeypatch.setattr(llm, "_estimate_tokens", lambda m: 200_000)          # > CLEAR_TRIGGER
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    calls = {"send": 0, "compact": 0}

    def flaky_send(params, stream):
        calls["send"] += 1
        if calls["send"] == 1:
            raise _Fake429()
        return FakeResponse()

    monkeypatch.setattr(llm, "_send_request", flaky_send)
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: calls.update(compact=calls["compact"] + 1) or True)

    resp = llm.llm_response([user("x" * 100)], stream=False)
    assert resp is not None
    assert calls["send"] == 2      # failed once, retried once
    assert calls["compact"] == 1   # one forced compaction


def test_reactive_429_small_context_reraises(monkeypatch):
    """A 429 on a SMALL request is a transient/quota limit compaction can't fix —
    it surfaces WITHOUT destroying history (no compaction)."""
    monkeypatch.setattr(llm, "_effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(llm, "_estimate_tokens", lambda m: 500)   # <= CLEAR_TRIGGER
    monkeypatch.setattr(llm, "_compact_attempts", 0)
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: (_ for _ in ()).throw(_Fake429()))
    compacted = {"n": 0}
    monkeypatch.setattr(llm, "_compact", lambda m, **kw: compacted.update(n=compacted["n"] + 1) or True)
    with pytest.raises(llm.APIStatusError):
        llm.llm_response([user("hi")], stream=False)
    assert compacted["n"] == 0   # never compacted a small transient 429
    assert compacted["n"] == 0     # never tried to compact for a 500


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
