"""Tests for the Task* store — CC's coordination substrate (replaces todo_write).

Pins the parts that make it a coordination substrate rather than a plan renderer:
id-keyed persistence, the symmetric blocks/blockedBy graph, emergent auto-unblock
(completing a blocker frees its dependents with no cross-file write), owner claims,
and delete cleaning up dangling references. Plus the four tool wrappers.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import tasks
from minicc.tools import task_create, task_get, task_list, task_update
from minicc.tools import TOOLS, TOOL_HANDLERS


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # tasks write under cwd/.minicc/tasks
    yield tmp_path


# ─── store: create / ids / persistence ────────────────────────────────────────

def test_create_assigns_sequential_ids_and_persists():
    a = tasks.create("first", "do A")
    b = tasks.create("second", "do B")
    assert (a["id"], b["id"]) == ("1", "2")
    assert a["status"] == "pending" and a["owner"] == ""
    # persisted to disk, re-readable (not conversation state — the CC difference)
    assert tasks.get("1")["subject"] == "first"
    assert [t["id"] for t in tasks.list_all()] == ["1", "2"]


def test_gitignore_written():
    tasks.create("x", "y")
    assert (tasks._dir().parent / ".gitignore").read_text() == "*\n"


# ─── status, owner, metadata merge ────────────────────────────────────────────

def test_update_status_owner_and_metadata_merge():
    tasks.create("t", "d")
    tasks.update("1", status="in_progress", owner="researcher",
                 metadata={"k": "v", "drop": "x"})
    tasks.update("1", metadata={"drop": None, "k2": "v2"})  # null deletes a key
    rec = tasks.get("1")
    assert rec["status"] == "in_progress" and rec["owner"] == "researcher"
    assert rec["metadata"] == {"k": "v", "k2": "v2"}


def test_update_missing_task_returns_none():
    assert tasks.update("99", status="completed") is None
    assert tasks.get("99") is None


# ─── the dependency graph + emergent auto-unblock ─────────────────────────────

def test_blocks_are_symmetric():
    tasks.create("setup", "d")     # #1
    tasks.create("build", "d")     # #2
    tasks.update("1", add_blocks=["2"])  # #1 blocks #2
    assert tasks.get("1")["blocks"] == ["2"]
    assert tasks.get("2")["blockedBy"] == ["1"]  # inverse written automatically


def test_add_blocked_by_writes_inverse():
    tasks.create("a", "d"); tasks.create("b", "d")
    tasks.update("2", add_blocked_by=["1"])   # #2 blockedBy #1
    assert tasks.get("2")["blockedBy"] == ["1"]
    assert tasks.get("1")["blocks"] == ["2"]


def test_auto_unblock_is_emergent():
    tasks.create("dep", "d")       # #1
    tasks.create("main", "d")      # #2
    tasks.update("2", add_blocked_by=["1"])
    # #2 is blocked while #1 is open
    assert tasks.open_blockers(tasks.get("2")) == ["1"]
    # completing #1 frees #2 WITHOUT touching #2's file (emergent)
    tasks.update("1", status="completed")
    assert tasks.open_blockers(tasks.get("2")) == []


def test_dangling_dep_id_skipped():
    tasks.create("only", "d")
    tasks.update("1", add_blocks=["nope"])  # no such task
    assert tasks.get("1")["blocks"] == []   # silently skipped, not crashed


def test_delete_removes_file_and_cleans_references():
    tasks.create("a", "d"); tasks.create("b", "d")
    tasks.update("1", add_blocks=["2"])
    tasks.update("1", status="deleted")
    assert tasks.get("1") is None
    assert tasks.get("2")["blockedBy"] == []  # dangling ref cleaned up
    assert [t["id"] for t in tasks.list_all()] == ["2"]


# ─── tool wrappers ────────────────────────────────────────────────────────────

def test_tools_registered_and_todo_write_gone():
    names = {t["name"] for t in TOOLS}
    assert {"task_create", "task_list", "task_get", "task_update"} <= names
    assert "todo_write" not in names
    for n in ("task_create", "task_list", "task_get", "task_update"):
        assert n in TOOL_HANDLERS


def test_tool_create_list_get_update_roundtrip():
    assert task_create.task_create("Fix bug", "the login bug") == \
        "Created task #1: Fix bug (pending)"
    out = task_list.task_list()
    assert "☐ #1 Fix bug" in out
    assert task_update.task_update("1", status="in_progress", owner="me").startswith("Updated:")
    assert "▶ #1 Fix bug  owner=me" in task_list.task_list()
    detail = task_get.task_get("1")
    assert "#1 [in_progress] Fix bug" in detail
    # unknown id is a value, not a crash
    assert task_get.task_get("99") == "Error: no task #99."
    assert task_update.task_update("99") == "Error: no task #99."


def test_tool_list_empty_and_delete():
    assert task_list.task_list() == "tasks: (none)"
    task_create.task_create("x", "y")
    assert task_update.task_update("1", status="deleted") == "Deleted task #1."
    assert task_list.task_list() == "tasks: (none)"
