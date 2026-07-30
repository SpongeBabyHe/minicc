"""Unit tests for session persistence — focus on the serialization round-trip."""

import os

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import json
import uuid

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from minicc import context_management as compact, sessions
from minicc.context_management import eviction


def _history():
    """A realistic mixed history: str content, SDK-object content, dict content."""
    return [
        {"role": "user", "content": "read x.py"},
        {
            "role": "assistant",
            "content": [
                TextBlock(text="Reading it.", type="text", citations=None),
                ToolUseBlock(id="t1", name="read_file", input={"path": "x.py"}, type="tool_use"),
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "..."}]},
    ]


def test_serialize_is_json_clean():
    msgs = sessions._serialize_messages(_history())
    # whole thing must json round-trip
    blob = json.dumps(msgs)
    assert json.loads(blob) == msgs


def test_sdk_blocks_become_api_clean_dicts():
    msgs = sessions._serialize_messages(_history())
    assistant = msgs[1]["content"]
    text_block, tool_block = assistant[0], assistant[1]

    assert text_block == {"type": "text", "text": "Reading it."}        # no citations:None
    assert tool_block == {
        "type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x.py"},
    }                                                                    # no caller:None


def test_strings_and_dicts_pass_through():
    msgs = sessions._serialize_messages(_history())
    assert msgs[0] == {"role": "user", "content": "read x.py"}           # str untouched
    assert msgs[2]["content"][0]["type"] == "tool_result"                # dict untouched


def test_unknown_block_fails_loud():
    # content only ever holds dicts or SDK blocks; an unexpected type must raise
    # (not silently save a dead repr that can't round-trip back to the API)
    with pytest.raises(TypeError, match="un-serializable"):
        sessions._serialize_messages([{"role": "assistant", "content": [object()]}])


def test_append_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # sessions write under cwd/.minicc
    sid = "20260616_120000"
    for m in _history():
        sessions.append_message(sid, m)

    loaded = sessions.load(sid)
    assert loaded == sessions._serialize_messages(_history())
    # self-ignoring dir was created
    assert (tmp_path / ".minicc" / ".gitignore").read_text() == "*\n"


def test_meta_flag_marks_expansion_record_only(tmp_path, monkeypatch):
    """A slash expansion persists as CC's two-record pair: tags message, then
    the expanded content with `meta: true` on the RECORD (CC's isMeta). The
    flag never reaches the replayed API message."""
    monkeypatch.chdir(tmp_path)
    sid = "20260717_090000"
    sessions.append_message(sid, {"role": "user", "content": "<command-name>/x</command-name>"})
    sessions.append_message(sid, {"role": "user", "content": "expanded body"}, meta=True)

    raw = [json.loads(l) for l in
           (tmp_path / ".minicc" / "sessions" / f"{sid}.jsonl").read_text().splitlines()]
    assert "meta" not in raw[0] and raw[1]["meta"] is True
    # replayed history carries plain API messages — no meta key leaks
    assert sessions.load(sid) == [
        {"role": "user", "content": "<command-name>/x</command-name>"},
        {"role": "user", "content": "expanded body"},
    ]


def test_compaction_boundary_reconstructs(tmp_path, monkeypatch):
    """load replays: msg events append, a compact event RESETS to its state — so
    resume yields [summary] + kept tail + anything appended after the boundary."""
    monkeypatch.chdir(tmp_path)
    sid = "s1"
    sessions.append_message(sid, {"role": "user", "content": "m0"})
    sessions.append_message(sid, {"role": "assistant", "content": "a0"})
    sessions.append_message(sid, {"role": "user", "content": "m1"})
    post = [                                          # post-compaction working set
        {"role": "user", "content": "[Earlier conversation summary]\n\nS"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "m1"},
    ]
    sessions.log_compaction(sid, post)
    sessions.append_message(sid, {"role": "assistant", "content": "a1"})

    assert sessions.load(sid) == post + [{"role": "assistant", "content": "a1"}]


def test_compaction_is_append_only_lossless(tmp_path, monkeypatch):
    """A compaction only APPENDS a boundary event — the raw msg events stay on disk
    (line count only grows), unlike the old overwrite-on-save that dropped them."""
    monkeypatch.chdir(tmp_path)
    sid = "s2"
    for i in range(3):
        sessions.append_message(sid, {"role": "user", "content": f"m{i}"})
    path = tmp_path / ".minicc" / "sessions" / f"{sid}.jsonl"
    before = len(path.read_text().splitlines())

    sessions.log_compaction(sid, [{"role": "user", "content": "S"}])
    after = len(path.read_text().splitlines())

    assert after == before + 1                        # only grew
    text = path.read_text()
    assert '"m0"' in text and '"m1"' in text and '"m2"' in text  # raw events intact


def test_context_edit_persists_output_and_replays_delta(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sid = "s-edit"
    original = "full result\n" + ("X" * 10_000)
    working = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool/../1",
                    "name": "read_file",
                    "input": {"path": "x.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool/../1",
                    "content": original,
                }
            ],
        },
    ]
    for message in working:
        sessions.append_message(sid, message)

    result = compact.evict_old_tool_results(
        working,
        min_savings_tokens=0,
        keep_recent=0,
        session_id=sid,
    )

    assert result.count == 1
    replacement = working[2]["content"][0]["content"]
    assert replacement.startswith("<persisted-output>\n")
    output_path = eviction._tool_result_output_path(
        sid,
        "tool/../1",
        original,
    )
    assert output_path.read_text(encoding="utf-8") == original
    assert output_path.is_relative_to(tmp_path / ".minicc" / "tool_outputs")

    events = [
        json.loads(line)
        for line in sessions.path(sid).read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["t"] == "context_edit"
    assert original in events[2]["m"]["content"][0]["content"]
    assert sessions.load(sid) == working
    assert sessions.load_upto(sid, 3)[2]["content"][0]["content"] == original


def test_latest_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sessions.latest_id() is None
    sessions.append_message("20260616_100000", {"role": "user", "content": "a"})
    sessions.append_message("20260616_110000", {"role": "user", "content": "b"})
    assert sessions.latest_id() == "20260616_110000"


def test_new_id_is_unique_uuid4():
    first = sessions.new_id()
    second = sessions.new_id()

    assert first != second
    assert uuid.UUID(first).version == 4
    assert uuid.UUID(second).version == 4


@pytest.mark.parametrize(
    "session_id",
    (
        "",
        "../escape",
        "../../tmp/escape",
        "/absolute",
        ".hidden",
        "has space",
    ),
)
def test_invalid_session_ids_are_rejected(tmp_path, monkeypatch, session_id):
    monkeypatch.chdir(tmp_path)

    if session_id:
        with pytest.raises(sessions.InvalidSessionIdError):
            sessions.path(session_id)
    else:
        with pytest.raises(sessions.InvalidSessionIdError):
            sessions.load(session_id)
    with pytest.raises(sessions.InvalidSessionIdError):
        sessions.append_message(
            session_id,
            {"role": "user", "content": "must not escape"},
        )

    assert not (tmp_path.parent / "escape.jsonl").exists()


def test_load_missing_raises_not_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(sessions.SessionNotFoundError, match="was not found"):
        sessions.load("nope")


def test_load_valid_empty_transcript(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / ".minicc" / "sessions" / "empty.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"")

    assert sessions.load("empty") == []


def test_load_ignores_only_truncated_final_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sessions.append_message("s", {"role": "user", "content": "complete"})
    transcript = sessions.path("s")
    with transcript.open("ab") as file:
        file.write(b'{"t":"msg","m":{"role":"assistant","content":"cut')

    assert sessions.load("s") == [{"role": "user", "content": "complete"}]
    assert sessions.event_count("s") == 1


def test_load_accepts_valid_final_line_without_newline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / ".minicc" / "sessions" / "s.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"t":"msg","m":{"role":"user","content":"complete"}}',
        encoding="utf-8",
    )

    assert sessions.load("s") == [{"role": "user", "content": "complete"}]


def test_load_rejects_corrupt_complete_final_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sessions.append_message("s", {"role": "user", "content": "complete"})
    with sessions.path("s").open("ab") as file:
        file.write(b'{"t":"msg","m":\n')

    with pytest.raises(sessions.SessionCorruptError, match="line 2"):
        sessions.load("s")


def test_load_rejects_corrupt_middle_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / ".minicc" / "sessions" / "s.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(
        b'{"t":"msg","m":{"role":"user","content":"first"}}\n'
        b'not-json\n'
        b'{"t":"msg","m":{"role":"assistant","content":"last"}}\n'
    )

    with pytest.raises(sessions.SessionCorruptError, match="line 2"):
        sessions.load("s")


def test_session_counters_are_durable_and_not_replayed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sessions.append_message("s", {"role": "user", "content": "hello"})
    sessions.record_counter("s", "web_searches", 3)
    sessions.record_counter("s", "subagent_spawns")

    assert sessions.counter_totals("s") == {
        "web_searches": 3,
        "subagent_spawns": 1,
    }
    assert sessions.load("s") == [{"role": "user", "content": "hello"}]


# ─── conversation rewind: event_count / load_upto / rewind event ─────────────
def test_event_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sessions.event_count("s") == 0
    sessions.append_message("s", {"role": "user", "content": "a"})
    sessions.append_message("s", {"role": "assistant", "content": "b"})
    assert sessions.event_count("s") == 2
    sessions.log_compaction("s", [{"role": "user", "content": "S"}])
    assert sessions.event_count("s") == 3          # compact is an event too


def test_load_upto_replays_partial(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for i in range(4):
        sessions.append_message("s", {"role": "user", "content": f"m{i}"})
    assert sessions.load_upto("s", 2) == [
        {"role": "user", "content": "m0"},
        {"role": "user", "content": "m1"},
    ]
    assert sessions.load_upto("s", 0) == []
    with pytest.raises(sessions.SessionNotFoundError):
        sessions.load_upto("nope", 2)


def test_load_upto_works_across_a_compaction(tmp_path, monkeypatch):
    """The rewind target may predate a compaction — replay reconstructs the
    pre-compaction working set from the raw msg events (the transcript's point)."""
    monkeypatch.chdir(tmp_path)
    sessions.append_message("s", {"role": "user", "content": "m0"})       # event 1
    sessions.append_message("s", {"role": "assistant", "content": "a0"})  # event 2
    sessions.log_compaction("s", [{"role": "user", "content": "S"}])      # event 3
    sessions.append_message("s", {"role": "assistant", "content": "a1"})  # event 4
    # rewind target = before the compaction: raw pre-compaction state
    assert sessions.load_upto("s", 2) == [
        {"role": "user", "content": "m0"},
        {"role": "assistant", "content": "a0"},
    ]
    # sanity: full replay applies the compact reset then the tail
    assert sessions.load("s") == [
        {"role": "user", "content": "S"},
        {"role": "assistant", "content": "a1"},
    ]


def test_rewind_event_resets_on_load(tmp_path, monkeypatch):
    """A rewind event has compact's reset semantics: load lands on the rewound
    state, while the rewound-away msg events stay on disk (append-only)."""
    monkeypatch.chdir(tmp_path)
    sessions.append_message("s", {"role": "user", "content": "m0"})
    sessions.append_message("s", {"role": "assistant", "content": "a0"})
    sessions.append_message("s", {"role": "user", "content": "m1"})
    rewound = [{"role": "user", "content": "m0"}]     # state as of event 1
    sessions.log_rewind("s", rewound)
    sessions.append_message("s", {"role": "assistant", "content": "a-new"})

    assert sessions.load("s") == rewound + [{"role": "assistant", "content": "a-new"}]
    path = tmp_path / ".minicc" / "sessions" / "s.jsonl"
    assert '"m1"' in path.read_text()                  # rewound-away msg still on disk


# ─── ts + usage fields (CC-transcript parity; enables process analysis) ──────

def test_append_message_records_ts_and_usage(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from minicc import sessions

    monkeypatch.chdir(tmp_path)
    sid = "20990101_000000"
    sessions.append_message(sid, {"role": "user", "content": "hi"})
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=20,
        cache_read_input_tokens=30, cache_creation_input_tokens=40,
    )
    sessions.append_message(sid, {"role": "assistant", "content": "yo"}, usage=usage)

    lines = [json.loads(l) for l in sessions.path(sid).read_text().splitlines()]
    assert all("ts" in e for e in lines)          # every msg carries a timestamp
    assert "usage" not in lines[0]                # user msg: no usage
    assert lines[1]["usage"] == {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40,
    }
    # replay ignores the new keys (old readers / mixed transcripts stay loadable)
    assert sessions.load(sid) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]
