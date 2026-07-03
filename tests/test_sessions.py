"""Unit tests for session persistence — focus on the serialization round-trip."""

import os

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import json

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from minicc import sessions


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


def test_latest_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sessions.latest_id() is None
    sessions.append_message("20260616_100000", {"role": "user", "content": "a"})
    sessions.append_message("20260616_110000", {"role": "user", "content": "b"})
    assert sessions.latest_id() == "20260616_110000"


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sessions.load("nope") is None


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
    assert sessions.load_upto("nope", 2) is None


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
