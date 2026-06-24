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


def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # sessions write under cwd/.minicc
    sid = "20260616_120000"
    sessions.save(sid, _history(), "test-model")

    loaded = sessions.load(sid)
    assert loaded == sessions._serialize_messages(_history())
    # self-ignoring dir was created
    assert (tmp_path / ".minicc" / ".gitignore").read_text() == "*\n"


def test_latest_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sessions.latest_id() is None
    sessions.save("20260616_100000", _history(), "m")
    sessions.save("20260616_110000", _history(), "m")
    assert sessions.latest_id() == "20260616_110000"


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert sessions.load("nope") is None
