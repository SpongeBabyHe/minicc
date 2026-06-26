"""Unit tests for file checkpoint / rewind (code-only, disk-backed)."""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from pathlib import Path

import pytest

from minicc import checkpoints


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # checkpoints write under cwd/.minicc
    checkpoints.reset()
    yield
    checkpoints.reset()


def test_revert_modified_file():
    f = Path("a.txt")
    f.write_text("original")
    checkpoints.start(1, "edit a")
    checkpoints.before_write("a.txt")
    f.write_text("modified")
    assert checkpoints.restore_files(1) == (1, [])
    assert f.read_text() == "original"


def test_new_file_deleted_on_rewind():
    checkpoints.start(1, "create b")
    checkpoints.before_write("b.txt")   # doesn't exist → ABSENT
    Path("b.txt").write_text("new")
    checkpoints.restore_files(1)
    assert not Path("b.txt").exists()


def test_newest_first_restores_oldest():
    f = Path("c.txt")
    f.write_text("v0")
    checkpoints.start(1, "t1"); checkpoints.before_write("c.txt"); f.write_text("v1")
    checkpoints.start(2, "t2"); checkpoints.before_write("c.txt"); f.write_text("v2")
    checkpoints.restore_files(1)        # revert to before turn 1
    assert f.read_text() == "v0"


def test_backup_once_per_turn():
    f = Path("d.txt")
    f.write_text("orig")
    checkpoints.start(1, "t")
    checkpoints.before_write("d.txt"); f.write_text("first")
    checkpoints.before_write("d.txt"); f.write_text("second")   # 2nd backup skipped
    checkpoints.restore_files(1)
    assert f.read_text() == "orig"


def test_restore_points_only_changing_turns():
    Path("e.txt").write_text("x")
    checkpoints.start(1, "read only")
    checkpoints.start(2, "edits e"); checkpoints.before_write("e.txt"); Path("e.txt").write_text("y")
    assert checkpoints.restore_points() == [(2, "edits e")]


def test_restore_files_unknown_turn_returns_none():
    assert checkpoints.restore_files(99) is None


def test_before_write_noop_without_checkpoint():
    Path("g.txt").write_text("x")
    checkpoints.before_write("g.txt")   # no active checkpoint → no crash, no backup
    assert checkpoints.restore_points() == []


def test_content_lives_on_disk():
    f = Path("h.txt")
    f.write_text("original")
    checkpoints.start(1, "t"); checkpoints.before_write("h.txt"); f.write_text("mod")
    backups = list(Path(".minicc/checkpoints/1").glob("*"))
    assert backups and backups[0].read_bytes() == b"original"


def test_multiple_files_one_turn():
    a = Path("a.txt"); a.write_text("a0")
    b = Path("b.txt"); b.write_text("b0")
    checkpoints.start(1, "edit a and b")
    checkpoints.before_write("a.txt"); a.write_text("a1")
    checkpoints.before_write("b.txt"); b.write_text("b1")
    assert checkpoints.restore_files(1) == (2, [])
    assert a.read_text() == "a0" and b.read_text() == "b0"


def test_mixed_new_and_modified_one_turn():
    m = Path("mod.txt"); m.write_text("orig")
    checkpoints.start(1, "t")
    checkpoints.before_write("mod.txt"); m.write_text("changed")
    checkpoints.before_write("created.txt"); Path("created.txt").write_text("new")   # ABSENT
    checkpoints.restore_files(1)
    assert m.read_text() == "orig"
    assert not Path("created.txt").exists()


def test_rewind_middle_turn_reverts_from_there_up():
    f = Path("f.txt"); f.write_text("v0")
    checkpoints.start(1, "t1"); checkpoints.before_write("f.txt"); f.write_text("v1")
    g = Path("g.txt"); g.write_text("g0")
    checkpoints.start(2, "t2"); checkpoints.before_write("g.txt"); g.write_text("g1")
    checkpoints.start(3, "t3"); checkpoints.before_write("f.txt"); f.write_text("v3")
    checkpoints.restore_files(2)        # revert turns 2+3, keep turn 1
    assert f.read_text() == "v1"        # turn 1's edit kept; turn 3's reverted to pre-turn-2
    assert g.read_text() == "g0"        # turn 2's edit reverted


def test_partial_rewind_keeps_earlier_edit():
    f = Path("f.txt"); f.write_text("v0")
    checkpoints.start(1, "t1"); checkpoints.before_write("f.txt"); f.write_text("v1")
    checkpoints.start(2, "t2"); checkpoints.before_write("f.txt"); f.write_text("v2")
    checkpoints.restore_files(2)        # revert only turn 2
    assert f.read_text() == "v1"


def test_binary_content_roundtrip():
    f = Path("bin.dat"); f.write_bytes(b"\x00\x01\x02\xff\xfe")
    checkpoints.start(1, "t"); checkpoints.before_write("bin.dat"); f.write_bytes(b"changed")
    checkpoints.restore_files(1)
    assert f.read_bytes() == b"\x00\x01\x02\xff\xfe"


def test_subdirectory_path():
    d = Path("pkg/sub"); d.mkdir(parents=True)
    f = d / "mod.py"; f.write_text("orig")
    checkpoints.start(1, "t"); checkpoints.before_write("pkg/sub/mod.py"); f.write_text("changed")
    checkpoints.restore_files(1)
    assert f.read_text() == "orig"


def test_stack_truncated_after_rewind():
    Path("a.txt").write_text("a")
    checkpoints.start(1, "t1"); checkpoints.before_write("a.txt"); Path("a.txt").write_text("a1")
    checkpoints.start(2, "t2"); checkpoints.before_write("a.txt"); Path("a.txt").write_text("a2")
    checkpoints.restore_files(2)
    assert checkpoints.restore_points() == [(1, "t1")]   # turn 2 discarded, turn 1 remains


def test_double_rewind_is_graceful():
    Path("a.txt").write_text("a")
    checkpoints.start(1, "t1"); checkpoints.before_write("a.txt"); Path("a.txt").write_text("a1")
    assert checkpoints.restore_files(1) == (1, [])
    assert checkpoints.restore_files(1) is None          # already rewound past


def test_reset_removes_disk():
    Path("a.txt").write_text("a")
    checkpoints.start(1, "t"); checkpoints.before_write("a.txt"); Path("a.txt").write_text("a1")
    assert Path(".minicc/checkpoints/1").exists()
    checkpoints.reset()
    assert not Path(".minicc/checkpoints/1").exists()
    assert checkpoints.restore_points() == []


def test_memory_stays_flat():
    f = Path("big.txt"); f.write_text("X" * 10000)
    checkpoints.start(1, "t"); checkpoints.before_write("big.txt"); f.write_text("small")
    cp = checkpoints._stack[-1]
    assert cp["files"]["big.txt"] == "0"                 # an id, not the 10k bytes
    assert all(v is None or isinstance(v, str) for v in cp["files"].values())


def test_hook_fires_in_real_agent_loop(monkeypatch):
    """Drive the actual agent_loop (model response mocked) through a real
    write_file call, and confirm the checkpoint hook captured it for /rewind."""
    from minicc import agent, permissions

    permissions.reset(); permissions.preload(["write_file"])   # skip the approve prompt
    Path("target.py").write_text("ORIGINAL")

    class B:
        def __init__(self, **k):
            self.__dict__.update(k)

    responses = iter([
        B(stop_reason="tool_use", content=[
            B(type="tool_use", id="t1", name="write_file",
              input={"path": "target.py", "content": "REWRITTEN"})]),
        B(stop_reason="end_turn", content=[B(type="text", text="done")]),
    ])
    monkeypatch.setattr(agent, "llm_response", lambda *a, **k: next(responses))

    history = [{"role": "user", "content": "rewrite target"}]
    checkpoints.start(1, "rewrite target")
    agent.agent_loop(history)

    assert Path("target.py").read_text() == "REWRITTEN"        # agent actually wrote it
    assert checkpoints.restore_files(1) == (1, [])             # hook captured it
    assert Path("target.py").read_text() == "ORIGINAL"         # /rewind restores
    permissions.reset()


def test_restore_recreates_missing_parent_dir():
    # bug ①: a modified file whose parent dir was removed (e.g. bash `rm -rf`) must
    # not crash the rewind — recreate the dir and restore.
    import shutil
    Path("sub").mkdir()
    f = Path("sub/x.txt"); f.write_text("orig")
    checkpoints.start(1, "t"); checkpoints.before_write("sub/x.txt"); f.write_text("mod")
    shutil.rmtree("sub")
    assert checkpoints.restore_files(1) == (1, [])
    assert Path("sub/x.txt").read_text() == "orig"


def test_missing_backup_is_reported_not_crash():
    # bug ②: if a backup file is gone, report it in failed_paths, don't abort.
    f = Path("y.txt"); f.write_text("orig")
    checkpoints.start(1, "t"); checkpoints.before_write("y.txt"); f.write_text("mod")
    cp = checkpoints._stack[-1]
    (cp["dir"] / cp["files"]["y.txt"]).unlink()      # corrupt: backup file vanishes
    assert checkpoints.restore_files(1) == (0, ["y.txt"])
