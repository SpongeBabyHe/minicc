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
    """Sub-agents get read-only tools; memory (a writer) must not be among them."""
    from minicc.tools import READ_ONLY_TOOLS, TOOLS
    assert "memory" in {t["name"] for t in TOOLS}
    assert "memory" not in {t["name"] for t in READ_ONLY_TOOLS}


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


# ─── the index rides the project-context cache layer (one breakpoint) ─────────
def test_memory_index_rides_project_layer():
    from minicc import llm
    llm.set_project_context("# Project\nP")
    llm.set_memory_index("# Auto-memory\nM")
    try:
        blocks = llm._build_system_block()
        project_block = blocks[1]["text"]
        assert "# Project" in project_block and "# Auto-memory" in project_block  # merged
        # merged into ONE block → system + project(+memory) = 2 breakpoints, no 5th
        assert sum(1 for b in blocks if "cache_control" in b) == 2
    finally:
        llm.set_project_context("")
        llm.set_memory_index("")


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
