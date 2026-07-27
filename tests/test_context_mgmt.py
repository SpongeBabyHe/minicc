"""Unit tests for L3/L4/L5 context management in minicc.llm.

These are deterministic and make NO real API calls — the Anthropic client's
`create` is monkeypatched. The focus is structural correctness:

- L4 _find_cut_index: cuts at an assistant boundary (works mid-turn)
- L4 _compact: produces a well-formed message list (no orphaned tool_result —
  the real API rule — plus minicc's conservative shape: first message is user,
  roles alternate; the API itself merges consecutive same-role messages)
- L4 _summarize: preserves system/tools/thinking, summarizes only the replaced
  prefix, and rejects incomplete/tool-call output
- L3 tool-result eviction: keeps recent eligible results; the savings guard skips
  an eviction that would free too little (don't break the cache for a small gain)
- two-band sizing: below the local trigger nothing fires; between it and the
  budget L3 evicts; over budget L4 compacts and skips eviction
- L5 thrashing guard: raises after MAX_COMPACT_ATTEMPTS instead of looping
"""

import os

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import context_management as compact, llm
from minicc.context_management import budget, eviction, summary


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
    """Assert minicc's compaction output is well-formed.

    The load-bearing API rule is that every tool_use is answered by a tool_result
    (an orphan 400s). Role ALTERNATION is minicc's own conservative shape, NOT an
    API requirement — the API merges consecutive same-role messages — but minicc's
    compaction output does alternate, so we assert it too as a shape check.
    """
    assert messages, "messages must be non-empty"
    assert messages[0]["role"] == "user", "first message must be user"

    # minicc's shape (not an API rule): roles alternate
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
    cut = summary._find_cut_index(msgs)
    assert cut is not None, "must find a cut point mid-turn"
    assert msgs[cut]["role"] == "assistant", "cut must land before an assistant"
    assert cut >= 2


def test_find_cut_index_returns_structural_boundary_only():
    """_find_cut_index judges STRUCTURE, not worth-it: a short history still
    yields its assistant boundary (the old `cut >= 2` message-count heuristic is
    gone — that decision moved to compact()'s token gate)."""
    msgs = [user("hi"), assistant_call("t0"), tool_result("t0")]
    assert summary._find_cut_index(msgs) == 1  # the assistant at index 1


def test_find_cut_index_none_when_no_assistant_boundary():
    """No assistant anywhere means there is no structurally safe cut."""
    assert summary._find_cut_index([user("only a user message")]) is None


def test_find_cut_index_falls_back_before_recent_window():
    """A missing boundary in the target window must retain more history, not fail."""
    msgs = [user("old"), {"role": "assistant", "content": "safe"}]
    msgs.extend(user(f"note-{index}") for index in range(7))
    assert summary._find_cut_index(msgs) == 1


# ─── L4: _compact structural validity (mock the API) ─────────────────────────
def test_compact_produces_valid_structure(monkeypatch):
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = single_turn(5)  # 11 messages
    ok = compact.compact(compact.ContextState(), msgs)
    assert ok is True
    assert_api_valid(msgs)               # ← no orphaned tool_result, alternation, etc.
    assert msgs[0]["role"] == "user"
    assert "summary" in msgs[0]["content"].lower()


def test_compact_keeps_recent_pairs_intact(monkeypatch):
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = single_turn(5)
    compact.compact(compact.ContextState(), msgs)
    # the tail after the summary must start with an assistant message
    assert msgs[1]["role"] == "assistant"
    assert_api_valid(msgs)


# ─── L4: _summarize preserves cacheable request settings ─────────────────────
def test_summarize_preserves_prefix_settings_and_appends_instruction(monkeypatch):
    captured = {}

    def capture_create(*args, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(llm.client.messages, "create", capture_create)
    msgs = single_turn(3)
    out = summary._summarize(msgs)

    assert "summary" in out.lower()
    # Same cacheable request settings as the live turn.
    assert captured["tools"] is llm.TOOLS
    assert isinstance(captured["system"], list) and captured["system"]
    assert captured["thinking"] == {"type": "adaptive"}
    # the instruction rides as the FINAL user message after the whole history.
    sent = captured["messages"]
    assert len(sent) == len(msgs) + 1
    assert sent[-1]["role"] == "user"
    assert "## Current Work" in sent[-1]["content"][-1]["text"]  # _COMPACT_PROMPT body
    assert "Do not call tools" in sent[-1]["content"][-1]["text"]


def test_compact_summarizes_only_the_replaced_prefix(monkeypatch):
    captured = {}
    msgs = single_turn(5)
    original = list(msgs)
    cut = summary._find_cut_index(msgs)

    def capture_summary(prefix, **kwargs):
        captured["prefix"] = list(prefix)
        return "SUMMARY"

    monkeypatch.setattr(summary, "_summarize", capture_summary)
    assert compact.compact(compact.ContextState(), msgs) is True
    assert captured["prefix"] == original[:cut]
    assert original[cut:] == msgs[1:]


def test_auto_compact_chooses_wider_cut_to_reach_budget(monkeypatch):
    """The target cut may preserve the oversized block that triggered compaction.
    Choose the earliest later assistant boundary that can reclaim enough instead
    of making one doomed summary call and sending the over-budget request."""
    msgs = [
        user("tiny"),
        assistant_call("t0"),
        tool_result("t0", "X" * 700_000),
        assistant_call("t1"),
        tool_result("t1", "tail"),
        assistant_call("t2"),
        tool_result("t2", "end"),
    ]
    assert summary._find_cut_index(msgs) == 1
    assert (
        compact.estimate_tokens(msgs)
        > compact.effective_budget("claude-haiku-4-5")
    )
    original = list(msgs)
    captured = {}

    def summarize(prefix, **kwargs):
        captured["prefix"] = list(prefix)
        return "SUMMARY"

    monkeypatch.setattr(summary, "_summarize", summarize)
    assert compact.compact(
        compact.ContextState(),
        msgs,
        model="claude-haiku-4-5",
    ) is True
    assert captured["prefix"] == original[:3]
    assert compact.estimate_tokens(msgs) < compact.effective_budget(
        "claude-haiku-4-5"
    )


# ─── L4: never destroy history on an empty summary (regression) ──────────────
class _ToolOnlyResponse:
    """A response with a tool_use block and NO text — what the model may return
    when tools are in scope and it tool-calls instead of summarizing."""
    content = [FakeToolUse("t0")]
    usage = FakeUsage()
    stop_reason = "tool_use"


def test_summarize_returns_none_when_no_text(monkeypatch):
    calls = []

    def tool_only(*args, **kwargs):
        calls.append(kwargs)
        return _ToolOnlyResponse()

    monkeypatch.setattr(llm.client.messages, "create", tool_only)
    assert summary._summarize(single_turn(3)) is None
    assert len(calls) == 2
    assert calls[1]["tool_choice"] == {"type": "none"}
    assert calls[1]["max_tokens"] == summary._SUMMARY_RETRY_MAX_TOKENS


def test_summarize_retries_truncated_output(monkeypatch):
    calls = []

    class Truncated(FakeResponse):
        def __init__(self):
            super().__init__("partial")
            self.stop_reason = "max_tokens"

    responses = [Truncated(), FakeResponse("complete summary")]

    def create(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(llm.client.messages, "create", create)
    assert summary._summarize(single_turn(3)) == "complete summary"
    assert len(calls) == 2
    assert calls[1]["tool_choice"] == {"type": "none"}


def test_compact_refuses_to_wipe_history_on_empty_summary(monkeypatch):
    monkeypatch.setattr(llm.client.messages, "create", lambda *a, **k: _ToolOnlyResponse())
    msgs = single_turn(5)
    before = list(msgs)
    assert compact.compact(compact.ContextState(), msgs) is False
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
    compact.compact(compact.ContextState(), single_turn(5), system="SUBAGENT PROMPT", tools=sub_tools)
    assert captured["tools"] is sub_tools
    assert captured["system"][0]["text"] == "SUBAGENT PROMPT"


# ─── Prompt-caching breakpoint budget (CC-style grouping) ────────────────────
def test_tools_carry_no_cache_breakpoint():
    """Tools are cached via the system-prompt breakpoint (tools render first), so
    none of them carry their own cache_control — keeping us inside the 4/request
    budget. See tools/__init__.py."""
    from minicc.tools import TOOLS

    assert all("cache_control" not in t for t in TOOLS)


def test_request_stays_within_four_breakpoints(monkeypatch):
    """system + session + the rolling conversation marker = 3 of the API's 4
    cache breakpoints (one spare). CLAUDE.md / memory / skills are NOT prefix
    layers — they ride <system-reminder> messages (reminders.py, CC parity)."""
    llm.set_session_context("# Session context\n- cwd: /x")
    try:
        system_blocks = llm._build_system_block()
        sys_bps = sum(1 for b in system_blocks if "cache_control" in b)
        convo_bps = 1  # _cacheable marks the last message
        assert sys_bps == 2                 # system + session
        assert sys_bps + convo_bps <= 4     # within the budget, one spare
    finally:
        llm.set_session_context("")


def test_stable_prefix_ttl_conversation_stays_default(monkeypatch):
    """cache_ttl=1h applies to the STABLE prefix blocks only; the rolling
    conversation breakpoint keeps the default 5m (the API requires longer-TTL
    breakpoints to precede shorter ones — stable layers render first)."""
    monkeypatch.setattr(llm, "CACHE_TTL", "1h")
    llm.set_session_context("# S\ny")
    try:
        blocks = llm._build_system_block()
        assert all(
            b["cache_control"] == {"type": "ephemeral", "ttl": "1h"} for b in blocks
        )
        out = llm._cacheable([{"role": "user", "content": "hi"}])
        assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    finally:
        llm.set_session_context("")


def test_session_context_is_volatile_last(monkeypatch):
    """Session context is the LAST system block (so a change never busts the static
    prefix above it) and carries its own cache breakpoint; static SYSTEM stays first."""
    llm.set_session_context("# Session context\n- cwd: /x")
    try:
        blocks = llm._build_system_block()
        assert blocks[0]["text"] == llm.SYSTEM                     # static first
        assert blocks[-1]["text"].startswith("# Session context")  # volatile last
        assert "cache_control" in blocks[-1]
    finally:
        llm.set_session_context("")


def test_no_session_block_when_unset():
    """Unset (e.g. sub-agents / tests) → no session block appears."""
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
    assert "Platform:" in ctx
    # the date is NOT here: it rides the claudeMd reminder as # currentDate
    # (CC parity) — a date in a cached prefix would go stale mid-session
    assert "Date:" not in ctx


# ─── L3: eviction keeps recent N ─────────────────────────────────────────────
def _turn_with_big_results(n, size=500):
    """One user turn + n (tool_use, tool_result) pairs whose results are big
    enough that replacing them with the 75-char marker is a NET saving."""
    msgs = [user("q")]
    for i in range(n):
        msgs.append(assistant_call(f"t{i}"))
        msgs.append(tool_result(f"t{i}", "F" * size))
    return msgs


def test_evict_keeps_recent(monkeypatch):
    monkeypatch.setattr(eviction, "TOOL_RESULT_EVICTION_KEEP_RECENT", 2)
    msgs = _turn_with_big_results(6)  # 6 tool_results, big enough to save on evict
    evicted = compact.evict_old_tool_results(msgs, min_savings_tokens=0)
    assert evicted.count == 4  # 6 total - 2 kept
    # count non-evicted tool_results remaining
    live = sum(
        1
        for m in msgs
        if isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict)
        and b.get("type") == "tool_result"
        and b["content"] != compact.EVICTED_MARKER
    )
    assert live == 2


def test_default_eviction_policy_is_keep_five_and_save_twenty_k():
    below = _turn_with_big_results(6, size=70_000)
    assert not compact.evict_old_tool_results(below)

    above = _turn_with_big_results(6, size=90_000)
    result = compact.evict_old_tool_results(above)
    assert result.count == 1
    assert result.estimated_tokens_saved >= 20_000
    assert above[2]["content"][0]["content"] == compact.EVICTED_MARKER


@pytest.mark.parametrize(
    "tool_name",
    ["agent", "memory", "skill", "task_create", "task_update"],
)
def test_evict_preserves_stateful_tool_results(tool_name):
    msgs = [
        user("q"),
        assistant_call("read", "read_file"),
        tool_result("read", "R" * 2_000),
        assistant_call("stateful", tool_name),
        tool_result("stateful", "S" * 2_000),
    ]

    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=0,
        keep_recent=0,
    )

    assert result.count == 1
    assert msgs[2]["content"][0]["content"] == compact.EVICTED_MARKER
    assert msgs[4]["content"][0]["content"] == "S" * 2_000


def test_evict_ignores_server_web_search_blocks():
    msgs = [
        user("search"),
        {
            "role": "assistant",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "srv-1",
                    "name": "web_search",
                    "input": {"query": "x"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srv-1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "encrypted_content": "opaque",
                        }
                    ],
                },
            ],
        },
    ]
    before = repr(msgs)

    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=0,
        keep_recent=0,
    )

    assert not result
    assert repr(msgs) == before


def test_eviction_plan_is_pure_and_reports_cache_suffix():
    msgs = _turn_with_big_results(3, size=2_000)
    before = repr(msgs)

    plan = compact.plan_tool_result_eviction(
        msgs,
        min_savings_tokens=0,
        keep_recent=1,
    )

    assert plan.count == 2
    assert plan.estimated_tokens_saved > 0
    assert plan.first_changed_message_index == 2
    assert plan.invalidated_suffix_tokens > 0
    assert repr(msgs) == before

    result = compact.apply_tool_result_eviction(msgs, plan)
    state = compact.ContextState()
    state.record_eviction(result)
    assert state.evictions == 1
    assert state.evicted_tool_results == 2
    assert state.evicted_tokens == result.estimated_tokens_saved
    assert state.last_eviction_suffix_tokens == plan.invalidated_suffix_tokens


def test_eviction_uses_same_utf8_estimator_as_context_size():
    """Non-ASCII output must not look four-to-six times smaller to L3 than L4."""
    msgs = [
        user("q"),
        assistant_call("t0"),
        tool_result("t0", "中" * 30_000),
    ]
    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=20_000,
        keep_recent=0,
    )
    assert result.count == 1
    assert result.estimated_tokens_saved >= 20_000


def test_eviction_does_not_mistake_marker_prefix_for_internal_marker():
    content = "<persisted-output>\n" + ("Y" * 100_000)
    msgs = [
        user("q"),
        assistant_call("t0"),
        tool_result("t0", content),
    ]
    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=0,
        keep_recent=0,
    )
    assert result.count == 1
    assert msgs[2]["content"][0]["content"] == compact.EVICTED_MARKER


def test_stale_eviction_plan_aborts_without_partial_edit():
    msgs = _turn_with_big_results(3, size=2_000)
    plan = compact.plan_tool_result_eviction(
        msgs,
        min_savings_tokens=0,
        keep_recent=0,
    )
    msgs[plan.edits[0].message_index]["content"][
        plan.edits[0].block_index
    ]["content"] = "changed after planning"

    result = compact.apply_tool_result_eviction(msgs, plan)

    assert not result
    assert msgs[4]["content"][0]["content"] == "F" * 2_000
    assert msgs[6]["content"][0]["content"] == "F" * 2_000


def test_main_session_eviction_aborts_if_spill_fails(monkeypatch):
    msgs = _turn_with_big_results(1, size=2_000)
    monkeypatch.setattr(
        eviction.sessions,
        "persist_tool_result_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    logged = []
    monkeypatch.setattr(
        eviction.sessions,
        "log_context_edit",
        lambda *args, **kwargs: logged.append((args, kwargs)),
    )

    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=0,
        keep_recent=0,
        session_id="s1",
    )

    assert not result
    assert msgs[2]["content"][0]["content"] == "F" * 2_000
    assert not logged


def test_main_session_eviction_aborts_if_replay_log_fails(monkeypatch):
    msgs = _turn_with_big_results(1, size=2_000)
    monkeypatch.setattr(
        eviction.sessions,
        "persist_tool_result_output",
        lambda *args, **kwargs: eviction.sessions.tool_result_output_path(
            args[0],
            args[1],
            args[2],
        ),
    )
    monkeypatch.setattr(
        eviction.sessions,
        "log_context_edit",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=0,
        keep_recent=0,
        session_id="s1",
    )

    assert not result
    assert msgs[2]["content"][0]["content"] == "F" * 2_000


def test_recovery_eviction_can_use_explicit_lossy_fallback(monkeypatch):
    msgs = _turn_with_big_results(1, size=2_000)
    monkeypatch.setattr(
        eviction.sessions,
        "persist_tool_result_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    logged = []
    monkeypatch.setattr(
        eviction.sessions,
        "log_context_edit",
        lambda session_id, replacements: logged.extend(replacements),
    )

    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=0,
        keep_recent=0,
        session_id="s1",
        allow_lossy_fallback=True,
    )

    assert result.count == 1
    assert msgs[2]["content"][0]["content"] == compact.EVICTED_MARKER
    assert logged == [
        {"tool_use_id": "t0", "content": compact.EVICTED_MARKER}
    ]


# ─── Window-relative compaction budget (CC-style trigger) ────────────────────
def test_effective_budget_reserves_output_and_buffer(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)
    b = compact.COMPACT_BUFFER_TOKENS
    out = compact.MAX_OUTPUT_TOKENS
    assert compact.effective_budget("claude-haiku-4-5") == 200_000 - out - b
    assert compact.effective_budget("claude-haiku-4-5-20251001") == 200_000 - out - b
    assert compact.effective_budget("claude-sonnet-4-6") == 1_000_000 - out - b
    assert compact.effective_budget("who-knows") == budget._DEFAULT_WINDOW - out - b


def test_effective_budget_honors_cc_window_and_percentage_overrides(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500000")
    assert compact.effective_budget("claude-sonnet-4-6") == (
        500_000 - compact.MAX_OUTPUT_TOKENS - compact.COMPACT_BUFFER_TOKENS
    )

    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "50")
    assert compact.effective_budget("claude-sonnet-4-6") == 250_000

    # Percentages above the default threshold cannot delay compaction.
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "100")
    assert compact.effective_budget("claude-sonnet-4-6") == (
        500_000 - compact.MAX_OUTPUT_TOKENS - compact.COMPACT_BUFFER_TOKENS
    )

    # A requested capacity cannot exceed the model's actual window.
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "9999999")
    monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    assert compact.effective_budget("claude-haiku-4-5") == (
        200_000 - compact.MAX_OUTPUT_TOKENS - compact.COMPACT_BUFFER_TOKENS
    )


def test_over_budget_compacts_and_skips_eviction(monkeypatch):
    """Upper band: over the compaction budget → L4 compacts directly, and L3
    eviction is deliberately skipped that turn so the summary call reads a warm
    cache (no in-place rewrite right before compaction)."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 1)   # everything over
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    calls = {"compact": 0, "evict": 0}
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: calls.update(compact=calls["compact"] + 1) or True)
    monkeypatch.setattr(
        compact,
        "evict_old_tool_results",
        lambda m, min_savings_tokens=0: calls.update(
            evict=calls["evict"] + 1
        ) or 0,
    )

    llm.llm_response([user("x" * 100)], stream=False)
    assert calls["compact"] == 1
    assert calls["evict"] == 0


def test_midband_evicts_with_minimum_savings_guard(monkeypatch):
    """Lower band: trigger < size <= budget runs guarded L3, not L4."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 200_000)

    monkeypatch.setattr(
        compact,
        "TOOL_RESULT_EVICTION_TRIGGER_TOKENS",
        100_000,
    )
    monkeypatch.setattr(
        compact,
        "TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS",
        20_000,
    )
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    calls = {"compact": 0, "evict": 0, "min_savings_tokens": None}
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: calls.update(compact=calls["compact"] + 1) or True)

    def fake_evict(m, min_savings_tokens=0):
        calls["evict"] += 1
        calls["min_savings_tokens"] = min_savings_tokens
        return 3
    monkeypatch.setattr(compact, "evict_old_tool_results", fake_evict)

    msgs = [user("hello")]
    state = compact.ContextState(
        last_input_tokens=150_000,
        last_message_tokens=compact.estimate_tokens(msgs),
    )
    llm.llm_response(msgs, stream=False, ctx=state)
    assert calls["compact"] == 0           # under budget → no compaction
    assert calls["evict"] == 1             # but over local trigger → evict
    assert calls["min_savings_tokens"] == 20_000


def test_below_clear_trigger_does_not_evict(monkeypatch):
    """Below the local trigger neither L3 nor L4 fires."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 200_000)

    monkeypatch.setattr(
        compact,
        "TOOL_RESULT_EVICTION_TRIGGER_TOKENS",
        100_000,
    )
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    calls = {"compact": 0, "evict": 0}
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: calls.update(compact=calls["compact"] + 1) or True)
    monkeypatch.setattr(
        compact,
        "evict_old_tool_results",
        lambda m, min_savings_tokens=0: calls.update(
            evict=calls["evict"] + 1
        ) or 0,
    )

    msgs = [user("hi")]
    state = compact.ContextState(
        last_input_tokens=50_000,
        last_message_tokens=compact.estimate_tokens(msgs),
    )
    llm.llm_response(msgs, stream=False, ctx=state)
    assert calls["compact"] == 0
    assert calls["evict"] == 0


def test_evict_skips_when_below_minimum_savings(monkeypatch):
    """The clear_at_least guard on NET savings: skip when the net gain (original
    minus the marker that replaces it) is below the minimum."""
    monkeypatch.setattr(eviction, "TOOL_RESULT_EVICTION_KEEP_RECENT", 2)
    msgs = _turn_with_big_results(6)   # 4 evictable * 500 chars; net ≈ 425 tokens
    before = [m["content"] for m in msgs]

    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=100_000,
    )
    assert not result
    assert [m["content"] for m in msgs] == before                 # untouched

    evicted = compact.evict_old_tool_results(msgs, min_savings_tokens=1)
    assert evicted.count == 4


# ─── L5: thrashing guard ─────────────────────────────────────────────────────
def test_thrash_guard_raises(monkeypatch):
    """When compaction can't get under budget, llm_response must raise after
    MAX_COMPACT_ATTEMPTS instead of looping forever."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 1)   # everything over
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: False)      # reduction impossible
    monkeypatch.setattr(llm.client.messages, "create", fake_create)

    msgs = [user("x" * 100)]
    st = compact.ContextState()               # the conversation's persistent state
    with pytest.raises(RuntimeError, match="thrashing"):
        for _ in range(compact.MAX_COMPACT_ATTEMPTS + 1):
            llm.llm_response(msgs, stream=False, ctx=st)


def test_no_thrash_when_under_budget(monkeypatch):
    """Under budget → no compaction, counter stays 0, no raise."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = [user("hi")]
    st = compact.ContextState()
    resp = llm.llm_response(msgs, stream=False, ctx=st)
    assert resp is not None
    assert st.compact_attempts == 0


# ─── Reactive compaction on a 413 (request-too-large) fallback ───────────────
class _Fake413(llm.APIStatusError):
    """A 413 without the SDK's heavy __init__ — only .status_code is read."""
    def __init__(self):
        self.status_code = 413


def test_reactive_compact_on_413(monkeypatch):
    """A 413 forces one compaction and retries the send once."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)  # no pre-compact
    calls = {"send": 0, "compact": 0, "recovery": None}

    def flaky_send(params, stream):
        calls["send"] += 1
        if calls["send"] == 1:
            raise _Fake413()
        return FakeResponse()

    monkeypatch.setattr(llm, "_send_request", flaky_send)

    def fake_compact(ctx, messages, **kwargs):
        calls["compact"] += 1
        calls["recovery"] = kwargs.get("recovery")
        return True

    monkeypatch.setattr(compact, "compact", fake_compact)

    resp = llm.llm_response([user("hi")], stream=False)
    assert resp is not None
    assert calls["send"] == 2      # failed once, retried once
    assert calls["compact"] == 1   # one forced compaction
    assert calls["recovery"] == "bytes"


class _Fake400(llm.APIStatusError):
    """A 400 with a settable message — only status_code + message are read."""
    def __init__(self, message):
        self.status_code = 400
        self.message = message


def test_reactive_compact_on_400_prompt_too_long(monkeypatch):
    """Token overflow (input alone > context window) is a 400 'prompt is too
    long', NOT a 413 (413 is the 32MB byte limit). The reactive path must catch
    it and compact."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)  # no pre-compact
    calls = {"send": 0, "compact": 0, "recovery": None}

    def flaky_send(params, stream):
        calls["send"] += 1
        if calls["send"] == 1:
            raise _Fake400("prompt is too long: 1200000 tokens > 1000000 maximum")
        return FakeResponse()

    monkeypatch.setattr(llm, "_send_request", flaky_send)

    def fake_compact(ctx, messages, **kwargs):
        calls["compact"] += 1
        calls["recovery"] = kwargs.get("recovery")
        return True

    monkeypatch.setattr(compact, "compact", fake_compact)

    resp = llm.llm_response([user("hi")], stream=False)
    assert resp is not None
    assert calls["send"] == 2 and calls["compact"] == 1
    assert calls["recovery"] == "tokens"


def test_reactive_400_detects_prompt_too_long_in_error_body(monkeypatch):
    class BodyOnly400(llm.APIStatusError):
        def __init__(self):
            self.status_code = 400
            self.body = {"error": {"message": "prompt is too long"}}

    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    sends = {"n": 0}

    def send(params, stream):
        sends["n"] += 1
        if sends["n"] == 1:
            raise BodyOnly400()
        return FakeResponse()

    monkeypatch.setattr(llm, "_send_request", send)
    monkeypatch.setattr(compact, "compact", lambda *args, **kwargs: True)
    assert llm.llm_response([user("hi")], stream=False) is not None
    assert sends["n"] == 2


def test_reactive_413_summary_does_not_resend_oversized_history(monkeypatch):
    """The recovery summary sends only a bounded prefix, not the rejected body."""
    monkeypatch.setattr(compact, "effective_budget", lambda model, **kwargs: 10_000_000)
    fixed_summary_bytes = summary._summary_request_footprint(
        [],
        focus=None,
        model=llm.MODEL,
        system=None,
        tools=None,
    )[1]
    summary_budget = fixed_summary_bytes + 5_000
    monkeypatch.setattr(
        summary,
        "_SUMMARY_REQUEST_BYTE_BUDGET",
        summary_budget,
    )
    monkeypatch.setattr(
        compact,
        "evict_old_tool_results",
        lambda messages, **kwargs: 0,
    )
    monkeypatch.setattr(compact, "recovery_needed", lambda *args: False)
    summary_request_sizes = []
    original_size = {"value": 0}

    def summary_create(*args, **kwargs):
        size = budget._serialized_size(kwargs["messages"])
        summary_request_sizes.append(size)
        assert budget._serialized_size(kwargs) <= summary_budget
        if size >= original_size["value"]:
            raise AssertionError("summary resent the oversized live history")
        return FakeResponse("short summary")

    monkeypatch.setattr(llm.client.messages, "create", summary_create)
    sends = {"n": 0}

    def send(params, stream):
        sends["n"] += 1
        if sends["n"] == 1:
            raise _Fake413()
        return FakeResponse()

    monkeypatch.setattr(llm, "_send_request", send)
    msgs = [
        user("A" * 1_000),
        assistant_call("t0"),
        tool_result("t0", "B" * 4_000),
        assistant_call("t1"),
        tool_result("t1", "C" * 1_000),
        assistant_call("t2"),
        tool_result("t2", "tail"),
    ]
    original_size["value"] = budget._serialized_size(msgs)

    assert llm.llm_response(msgs, stream=False) is not None
    assert sends["n"] == 2
    assert summary_request_sizes
    assert max(summary_request_sizes) < original_size["value"]


def test_reactive_repeats_bounded_compaction_before_live_retry(monkeypatch):
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(summary, "_SUMMARY_REQUEST_BYTE_BUDGET", 180)
    monkeypatch.setattr(
        compact,
        "evict_old_tool_results",
        lambda messages, **kwargs: 0,
    )
    compact_calls = {"n": 0}

    def shrink(ctx, messages, **kwargs):
        compact_calls["n"] += 1
        messages[0]["content"] = messages[0]["content"][: len(messages[0]["content"]) // 2]
        ctx.rebase(messages)
        return True

    monkeypatch.setattr(compact, "compact", shrink)
    sends = {"n": 0}

    def send(params, stream):
        sends["n"] += 1
        if sends["n"] == 1:
            raise _Fake413()
        assert budget._serialized_size(params["messages"]) <= 300
        return FakeResponse()

    monkeypatch.setattr(llm, "_send_request", send)
    assert llm.llm_response([user("X" * 1_000)], stream=False) is not None
    assert compact_calls["n"] > 1
    assert sends["n"] == 2


def test_reactive_skips_generic_400(monkeypatch):
    """A 400 that is NOT token overflow (e.g. a validation error) must propagate,
    not trigger a destructive compaction."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    compacted = {"n": 0}
    monkeypatch.setattr(
        llm, "_send_request",
        lambda p, s: (_ for _ in ()).throw(_Fake400("thinking blocks cannot be modified")),
    )
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: compacted.update(n=compacted["n"] + 1) or True)
    with pytest.raises(llm.APIStatusError):
        llm.llm_response([user("hi")], stream=False)
    assert compacted["n"] == 0   # never compacted for a non-overflow 400


def test_reactive_compact_gives_up_when_uncompactable(monkeypatch):
    """If compaction can't reduce (returns False), the 413 propagates."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: (_ for _ in ()).throw(_Fake413()))
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: False)
    with pytest.raises(llm.APIStatusError):
        llm.llm_response([user("hi")], stream=False)


def test_non_413_status_error_propagates(monkeypatch):
    """A non-413 API error is not treated as too-large; it propagates as-is."""
    class _Fake500(llm.APIStatusError):
        def __init__(self):
            self.status_code = 500

    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: (_ for _ in ()).throw(_Fake500()))
    compacted = {"n": 0}
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: compacted.update(n=compacted["n"] + 1) or True)
    with pytest.raises(llm.APIStatusError):
        llm.llm_response([user("hi")], stream=False)


# ─── 429 is not a context-size signal ────────────────────────────────────────
class _Fake429(llm.APIStatusError):
    """A 429 (rate limit); only .status_code is read."""
    def __init__(self):
        self.status_code = 429


@pytest.mark.parametrize("estimated_tokens", [500, 200_000])
def test_429_reraises_without_compaction(monkeypatch, estimated_tokens):
    """429 has several causes; large and small requests both preserve history."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)  # no pre-compact
    monkeypatch.setattr(compact, "estimate_tokens", lambda m: estimated_tokens)
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: (_ for _ in ()).throw(_Fake429()))
    compacted = {"n": 0}
    monkeypatch.setattr(compact, "compact", lambda c, m, **kw: compacted.update(n=compacted["n"] + 1) or True)
    with pytest.raises(llm.APIStatusError):
        llm.llm_response([user("hi")], stream=False)
    assert compacted["n"] == 0


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


# ─── adaptive thinking (CC parity: both CC arms ran with thinking on) ────────

def test_thinking_param_by_model():
    from minicc.llm import _thinking_param

    assert _thinking_param("claude-sonnet-4-6") == {"type": "adaptive"}
    assert _thinking_param("claude-opus-4-8") == {"type": "adaptive"}
    assert _thinking_param("claude-fable-5") == {"type": "adaptive"}
    assert _thinking_param("claude-haiku-4-5") is None  # pre-adaptive: omit entirely


def test_request_includes_thinking_for_adaptive_models(monkeypatch):
    from minicc import llm

    seen = {}

    def fake_send(params, stream):
        seen.update(params)

        class _U:
            input_tokens = output_tokens = 0
            cache_read_input_tokens = cache_creation_input_tokens = 0
            server_tool_use = None

        class _R:
            stop_reason, content, usage = "end_turn", [], _U()

        return _R()

    monkeypatch.setattr(llm, "_send_request", fake_send)
    # no _context_size patch needed: a fresh ContextState on a tiny message
    # estimates far below every trigger
    llm.llm_response([{"role": "user", "content": "hi"}], stream=False)
    assert seen.get("thinking") == {"type": "adaptive"}
    assert seen.get("max_tokens") == 16_000
    seen.clear()
    llm.llm_response([{"role": "user", "content": "hi"}], stream=False,
                     model="claude-haiku-4-5")
    assert "thinking" not in seen  # sub-agent model: no thinking param


# ─── R1 regression: per-conversation trigger state (the poisoning bug) ───────
def test_nested_call_does_not_poison_parent_trigger(monkeypatch):
    """Pre-R1, the trigger input was module-global: a nested llm_response (a
    sub-agent, web_fetch's extraction, /memory consolidate) overwrote the
    PARENT conversation's reading, silently skipping its next proactive
    compaction (executed repro: parent 950K read as 3K); and a fresh sub-agent
    inherited the parent's big value, spuriously entering the compaction branch
    on its first turn. ContextState is per conversation — both directions must
    stay fixed."""
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: FakeResponse())
    parent = compact.ContextState(
        last_input_tokens=950_000,
        last_message_tokens=0,
    )

    # a nested single-shot call (ctx omitted → its own throwaway state)
    llm.llm_response([user("nested one-shot")], stream=False)
    assert parent.last_input_tokens == 950_000     # parent's reading untouched

    # a fresh sub-agent conversation reads ITS OWN size, not the parent's
    sub = compact.ContextState()
    assert (
        sub.context_size([user("tiny")])
        < compact.TOOL_RESULT_EVICTION_TRIGGER_TOKENS
    )

    # the parent's own turn updates only the parent's state
    monkeypatch.setattr(compact, "effective_budget", lambda model: 10_000_000)
    llm.llm_response([user("parent turn")], stream=False, ctx=parent)
    assert parent.last_input_tokens == 10          # FakeUsage.input_tokens
    assert parent.last_message_tokens == compact.estimate_tokens(
        [user("parent turn")]
    )


def test_context_size_adds_new_message_growth_to_real_usage():
    baseline = [user("small")]
    grown = baseline + [user("X" * 20_000)]
    state = compact.ContextState()
    baseline_estimate = compact.estimate_tokens(baseline)
    state.record_input(100, baseline_estimate)

    assert state.context_size(grown) == (
        100 + compact.estimate_tokens(grown) - baseline_estimate
    )
    assert state.context_size(grown) > 100


# ─── L4: worth is decided after the summary, not by a magic floor ────────────
def test_giant_early_message_now_compacts(monkeypatch):
    """Regression for the `cut >= 2` misfire: a single huge early user message in
    a short conversation used to be refused (cut=1 < 2). It must now compact."""
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = [user("X" * 600_000), assistant_call("t0"), tool_result("t0")]
    before = len(str(msgs))
    ok = compact.compact(compact.ContextState(), msgs)
    assert ok is True
    assert msgs[0]["role"] == "user" and "summary" in msgs[0]["content"].lower()
    assert len(str(msgs)) < before                 # actually shrank
    assert_api_valid(msgs)


def test_tiny_prefix_is_checked_posthoc_not_rejected_by_a_magic_gate(monkeypatch):
    """A tiny prefix is attempted; only the measured replacement decides."""
    called = {"n": 0}
    monkeypatch.setattr(
        llm.client.messages, "create",
        lambda *a, **k: called.update(n=called["n"] + 1) or FakeResponse(),
    )
    # tiny prefix (["hi"]), a real assistant boundary at index 1
    msgs = [user("hi"), assistant_call("t0"), tool_result("t0")]
    assert compact.compact(compact.ContextState(), msgs) is False
    assert called["n"] == 1


def test_compact_refuses_when_result_not_smaller(monkeypatch):
    """Post-hoc guarantee: if the bulk is in the KEPT tail, summarizing the prefix
    doesn't shrink anything — refuse instead of growing/no-op-replacing history."""
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    # TINY prefix: summarizing it can't shrink the total (the summary is bigger
    # than "hi"), and the bulk sits in the KEPT tail — post-hoc must refuse.
    msgs = [user("hi"), assistant_call("t0"), tool_result("t0", "Z" * 600_000)]
    before = list(msgs)
    assert compact.compact(compact.ContextState(), msgs) is False
    assert msgs == before                            # history untouched


# ─── audit P1 fixes: eviction net-savings, rebase-after-compact ───────────────
def test_evict_refuses_when_marker_would_grow_context(monkeypatch):
    """Audit P1: the 75-char marker is longer than a small tool_result, so
    evicting many tiny results would GROW the context. The guard uses NET savings
    (original minus marker), not original size, and must refuse."""
    monkeypatch.setattr(eviction, "TOOL_RESULT_EVICTION_KEEP_RECENT", 3)
    msgs = [user("q")]
    for i in range(1004):
        msgs.append(assistant_call(f"t{i}"))
        msgs.append(tool_result(f"t{i}", "X" * 20))   # tiny — marker is bigger
    before = compact.estimate_tokens(msgs)
    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=compact.TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS,
    )
    assert not result
    assert compact.estimate_tokens(msgs) == before     # context did NOT grow


def test_evict_skips_individual_negative_savings_blocks(monkeypatch):
    monkeypatch.setattr(eviction, "TOOL_RESULT_EVICTION_KEEP_RECENT", 1)
    msgs = [
        user("q"),
        assistant_call("tiny"),
        tool_result("tiny", "x"),
        assistant_call("large"),
        tool_result("large", "Y" * 1_000),
        assistant_call("recent"),
        tool_result("recent", "Z" * 1_000),
    ]

    result = compact.evict_old_tool_results(
        msgs,
        min_savings_tokens=0,
    )
    assert result.count == 1
    assert msgs[2]["content"][0]["content"] == "x"
    assert msgs[4]["content"][0]["content"] == compact.EVICTED_MARKER
    assert msgs[6]["content"][0]["content"] == "Z" * 1_000


def test_compact_rebases_context_size_after_shrink(monkeypatch):
    """After compaction, the trigger baseline follows the smaller working set."""
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = [
        user("X" * 400_000),
        assistant_call("t0"),
        tool_result("t0"),
        assistant_call("t1"),
        tool_result("t1"),
    ]
    before = compact.estimate_tokens(msgs)
    ctx = compact.ContextState(
        last_input_tokens=before + 500,
        last_message_tokens=before,
    )
    assert compact.compact(ctx, msgs) is True
    assert ctx.last_message_tokens == compact.estimate_tokens(msgs)
    assert ctx.context_size(msgs) == ctx.last_input_tokens
    assert ctx.context_size(msgs) < before


def test_standing_precompact_veto_does_not_thrash(monkeypatch):
    """Audit P2: a PreCompact hook that vetoes every turn must NOT count toward
    the thrash guard — a veto is the user's choice (proceed uncompacted), not a
    failed compaction. So >MAX_COMPACT_ATTEMPTS vetoed turns must not raise."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 1)   # always over budget
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: FakeResponse())
    monkeypatch.setattr(compact, "compact", lambda *a, **k: None)        # vetoed every turn

    ctx = compact.ContextState()
    for _ in range(compact.MAX_COMPACT_ATTEMPTS + 3):   # well past the limit
        llm.llm_response([user("x" * 100)], stream=False, ctx=ctx)
    assert ctx.compact_attempts == 0                    # never counted a veto


def test_failed_compaction_still_thrashes(monkeypatch):
    """Complement: a compaction that ran but couldn't reduce (returns False, not
    None) DOES count — so a genuinely un-compactable history still trips the
    guard after MAX_COMPACT_ATTEMPTS."""
    monkeypatch.setattr(compact, "effective_budget", lambda model: 1)
    monkeypatch.setattr(llm, "_send_request", lambda params, stream: FakeResponse())
    monkeypatch.setattr(compact, "compact", lambda *a, **k: False)       # attempted, no reduction

    ctx = compact.ContextState()
    with pytest.raises(RuntimeError, match="thrashing"):
        for _ in range(compact.MAX_COMPACT_ATTEMPTS + 1):
            llm.llm_response([user("x" * 100)], stream=False, ctx=ctx)


def test_moderate_prefix_compacts(monkeypatch):
    """Audit P2: a ~4K-token prefix summarizes to ~100 tokens — clearly worth it.
    Any pre-summary magic floor can misclassify it, so only the post-hoc size
    check decides."""
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    msgs = [user("A" * 16_000), assistant_call("t0"), tool_result("t0", "z"),
            assistant_call("t1"), tool_result("t1", "z")]
    before = compact.estimate_tokens(msgs)
    assert compact.compact(compact.ContextState(), msgs) is True
    assert compact.estimate_tokens(msgs) < before
