"""Unit tests for auto-memory: the store, the tool, path safety, gating, and the
index cache layer."""

import os

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import memory
from minicc.tools import memory as memory_tool
from minicc import permissions


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the memory store to a temp dir (no git / ~/.minicc dependence)."""
    d = tmp_path / "memory"
    monkeypatch.setattr(memory, "store_dir", lambda: d)
    return d


# ─── store ops ───────────────────────────────────────────────────────────────
def test_create_view_round_trip(store):
    assert memory.create("/memories/foo.md", "hello\nworld") == (
        "File created successfully at: /memories/foo.md"
    )
    out = memory.view("/memories/foo.md")
    assert "hello" in out and "world" in out
    assert (store / "foo.md").read_text() == "hello\nworld"


def test_create_makes_nested_dirs(store):
    memory.create("/memories/sub/x.md", "deep")
    assert (store / "sub" / "x.md").read_text() == "deep"


def test_str_replace_unique(store):
    memory.create("/memories/a.md", "x=1\ny=2\n")
    assert memory.str_replace("/memories/a.md", "x=1", "x=9") == "The memory file has been edited."
    assert "x=9" in (store / "a.md").read_text()


def test_str_replace_rejects_missing_and_ambiguous(store):
    memory.create("/memories/a.md", "dup\ndup\n")
    assert "did not appear" in memory.str_replace("/memories/a.md", "nope", "x")
    assert "must be unique" in memory.str_replace("/memories/a.md", "dup", "x")


def test_str_replace_on_missing_file(store):
    assert "does not exist" in memory.str_replace("/memories/nope.md", "a", "b")


# ─── path traversal protection ───────────────────────────────────────────────
def test_path_traversal_blocked(store):
    assert "escapes" in memory.create("/memories/../escape.md", "bad")
    assert "escapes" in memory.view("/memories/../../etc/passwd")
    assert not (store.parent / "escape.md").exists()


# ─── index loading (project-context layer) ───────────────────────────────────
def test_load_index_empty_when_none(store):
    assert memory.load_index() == ""


def test_load_index_caps_lines(store, monkeypatch):
    monkeypatch.setattr(memory, "_INDEX_MAX_LINES", 3)
    memory.create("/memories/MEMORY.md", "l1\nl2\nl3\nl4\nl5")
    idx = memory.load_index()
    assert "l1" in idx and "l3" in idx and "l4" not in idx  # capped to 3 lines


def test_view_directory_listing(store):
    memory.create("/memories/MEMORY.md", "index")
    memory.create("/memories/debugging.md", "a fact")
    listing = memory.view("/memories")
    assert "MEMORY.md" in listing and "debugging.md" in listing


# ─── tool dispatch ───────────────────────────────────────────────────────────
def test_tool_dispatch(store):
    assert memory_tool.memory("create", "/memories/x.md", file_text="hi").startswith("File created")
    assert "hi" in memory_tool.memory("view", "/memories/x.md")
    assert memory_tool.memory("bogus", "/memories/x.md").startswith("Error: unknown command")


def test_memory_not_offered_to_subagents():
    """The read-only explore sub-agent must not carry memory (a writer)."""
    from minicc import agents
    from minicc.tools import TOOLS
    assert "memory" in {t["name"] for t in TOOLS}
    assert "memory" not in agents.resolve("explore").tools


# ─── gating (writes gated, view free) ────────────────────────────────────────
def test_memory_view_is_ungated():
    assert permissions.confirm("memory", {"command": "view", "path": "/memories"}) is True


def test_memory_writes_are_gated(monkeypatch):
    permissions.reset()
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert permissions.confirm(
        "memory", {"command": "create", "path": "/memories/x.md", "file_text": "y"}
    ) is False
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert permissions.confirm(
        "memory", {"command": "str_replace", "path": "/memories/x.md", "old_str": "a"}
    ) is True


def test_memory_is_preloadable_from_config():
    """memory is in GATED_TOOLS (single source of truth), so config can pre-approve
    its writes — same as write_file, unlike bash."""
    permissions.reset()
    applied = permissions.preload(["memory"])
    assert "memory" in applied
    # a write now skips the prompt (no input() needed — would raise OSError if asked)
    assert permissions.confirm(
        "memory", {"command": "create", "path": "/memories/x.md", "file_text": "y"}
    ) is True
    permissions.reset()


# ─── the index rides the claudeMd system-reminder (CC parity) ─────────────────
def test_memory_index_rides_claude_md_reminder(store):
    """The MEMORY.md index is delivered inside the claudeMd reminder with CC's
    exact label — not as a system-prompt cache layer (see reminders.py)."""
    from minicc import reminders

    memory.create("/memories/MEMORY.md", "- [Fact](f.md) — a hook")
    text = reminders._claude_md_text()
    assert "(user's auto-memory, persists across conversations):" in text
    assert "- [Fact](f.md) — a hook" in text
    assert str(store / "MEMORY.md") in text  # labeled with its real path


def test_delete_file_and_guards(store):
    memory.create("/memories/x.md", "fact")
    assert memory.delete("/memories/x.md") == "Successfully deleted /memories/x.md"
    assert not (store / "x.md").exists()
    assert "does not exist" in memory.delete("/memories/x.md")     # already gone
    assert "cannot delete" in memory.delete("/memories")           # root protected
    assert "escapes" in memory.delete("/memories/../../etc")       # traversal blocked


def test_memory_delete_is_gated(monkeypatch):
    permissions.reset()
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert permissions.confirm("memory", {"command": "delete", "path": "/memories/x.md"}) is False


def test_consolidate_runs_agent_loop_with_memory_tool_only(store, monkeypatch):
    """consolidate() = one agent_loop pass, memory tool only, and returns the
    final assistant text as the change summary."""
    import minicc.query_engine as agent_mod

    captured = {}

    def fake_loop(msgs, system=None, stream=True, tools=None, max_turns=None, indent="", session_id=None):
        captured["tools"] = [t["name"] for t in tools]
        captured["system"] = system
        captured["max_turns"] = max_turns
        msgs.append({
            "role": "assistant",
            "content": [type("T", (), {"type": "text", "text": "merged 2, deleted 1"})()],
        })

    monkeypatch.setattr(agent_mod, "agent_loop", fake_loop)
    out = memory.consolidate()
    assert captured["tools"] == ["memory"]          # no bash/write/etc in reach
    assert captured["max_turns"] == 15
    assert "memory maintainer" in captured["system"]
    assert out == "merged 2, deleted 1"


def test_disabled_blocks_writes_and_index(store):
    """`/memory off` → index isn't injected and writes refuse; reads still work."""
    memory.create("/memories/MEMORY.md", "hi")     # enabled by default
    assert memory.load_index() != ""
    memory.set_enabled(False)
    try:
        assert memory.load_index() == ""
        assert "disabled" in memory.create("/memories/x.md", "y")
        assert "disabled" in memory.str_replace("/memories/MEMORY.md", "hi", "bye")
        assert "hi" in memory.view("/memories/MEMORY.md")   # view still works
    finally:
        memory.set_enabled(True)                    # restore module state
