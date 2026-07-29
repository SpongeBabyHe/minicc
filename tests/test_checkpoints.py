"""Unit tests for file checkpoint / rewind (code-only, disk-backed)."""

import json
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from pathlib import Path

import pytest

from minicc import checkpoints


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # checkpoints write under cwd/.minicc
    checkpoints.activate("test-session")
    yield
    checkpoints._session_id = None
    checkpoints._checkpoints = []


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


def test_path_aliases_are_backed_up_only_once():
    target = Path("alias.txt")
    target.write_text("v0")
    checkpoints.start(1, "alias")
    checkpoints.before_write("alias.txt")
    target.write_text("v1")
    checkpoints.before_write("./alias.txt")
    target.write_text("v2")

    assert checkpoints.restore_files(1) == (1, [])
    assert target.read_text() == "v0"


def test_tilde_path_matches_file_tool_resolution(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = home / "tilde.txt"
    target.write_text("before")
    checkpoints.start(1, "tilde")
    checkpoints.before_write("~/tilde.txt")
    target.write_text("after")

    assert checkpoints.restore_files(1) == (1, [])
    assert target.read_text() == "before"


def test_restore_points_lists_every_turn_with_changed_files():
    """Every turn is a restore point (CC: one checkpoint per prompt); the entry
    shows which files that turn changed (empty for read-only turns)."""
    Path("e.txt").write_text("x")
    checkpoints.start(1, "read only")
    checkpoints.start(2, "edits e"); checkpoints.before_write("e.txt"); Path("e.txt").write_text("y")
    assert checkpoints.restore_points() == [
        (1, "read only", []),
        (2, "edits e", ["e.txt"]),
    ]


def test_events_at_returns_conversation_anchor():
    checkpoints.start(1, "a", events=0)
    checkpoints.start(2, "b", events=5)
    assert checkpoints.events_at(1) == 0
    assert checkpoints.events_at(2) == 5
    assert checkpoints.events_at(99) is None


def test_event_anchors_must_not_move_backwards():
    checkpoints.start(1, "a", events=5)

    with pytest.raises(ValueError, match="non-decreasing"):
        checkpoints.start(2, "b", events=4)

    assert checkpoints.last_turn() == 1


def test_restore_files_from_read_only_turn_reverts_later_turns():
    """Rewinding to a read-only turn undoes file changes made in LATER turns."""
    f = Path("r.txt")
    f.write_text("v0")
    checkpoints.start(1, "read only")                 # no file changes this turn
    checkpoints.start(2, "edit"); checkpoints.before_write("r.txt"); f.write_text("v1")
    restored, failed = checkpoints.restore_files(1)   # read-only turn is valid
    assert (restored, failed) == (1, [])
    assert f.read_text() == "v0"


def test_restore_files_unknown_turn_returns_none():
    assert checkpoints.restore_files(99) is None


def test_before_write_noop_without_checkpoint():
    Path("g.txt").write_text("x")
    checkpoints.before_write("g.txt")   # no active checkpoint → no crash, no backup
    assert checkpoints.restore_points() == []


def test_before_write_skips_non_regular_paths():
    Path("folder").mkdir()
    checkpoints.start(1, "directory")

    checkpoints.before_write("folder")

    assert checkpoints.restore_points() == [(1, "directory", [])]


def test_content_lives_on_disk():
    f = Path("h.txt")
    f.write_text("original")
    checkpoints.start(1, "t"); checkpoints.before_write("h.txt"); f.write_text("mod")
    backups = list(Path(".minicc/checkpoints/test-session/1").glob("*"))
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


def test_checkpoint_list_truncated_after_rewind():
    Path("a.txt").write_text("a")
    checkpoints.start(1, "t1"); checkpoints.before_write("a.txt"); Path("a.txt").write_text("a1")
    checkpoints.start(2, "t2"); checkpoints.before_write("a.txt"); Path("a.txt").write_text("a2")
    checkpoints.restore_files(2)
    assert checkpoints.restore_points() == [(1, "t1", ["a.txt"])]   # turn 2 discarded, turn 1 remains


def test_double_rewind_is_graceful():
    Path("a.txt").write_text("a")
    checkpoints.start(1, "t1"); checkpoints.before_write("a.txt"); Path("a.txt").write_text("a1")
    assert checkpoints.restore_files(1) == (1, [])
    assert checkpoints.restore_files(1) is None          # already rewound past


def test_memory_stays_flat():
    f = Path("big.txt"); f.write_text("X" * 10000)
    checkpoints.start(1, "t"); checkpoints.before_write("big.txt"); f.write_text("small")
    cp = checkpoints._checkpoints[-1]
    assert cp["files"]["big.txt"] == "0"                 # an id, not the 10k bytes
    assert all(v is None or isinstance(v, str) for v in cp["files"].values())
    assert set(cp) == {"turn", "query", "events", "files"}
    index = json.loads(
        Path(".minicc/checkpoints/test-session/index.json").read_text()
    )
    assert index["checkpoints"] == checkpoints._checkpoints


def test_checkpoint_index_and_backups_survive_resume():
    target = Path("resume.txt")
    target.write_text("before")
    checkpoints.start(1, "edit after restart", events=7)
    checkpoints.before_write(target)
    target.write_text("after")

    checkpoints.activate("other-session")
    assert checkpoints.restore_points() == []
    checkpoints.activate("test-session", resume=True)

    assert checkpoints.restore_points() == [
        (1, "edit after restart", ["resume.txt"])
    ]
    assert checkpoints.events_at(1) == 7
    assert checkpoints.last_turn() == 1
    assert checkpoints.restore_files(1) == (1, [])
    assert target.read_text() == "before"


def test_checkpoint_sessions_are_isolated():
    checkpoints.start(1, "session one")
    checkpoints.activate("session-two")
    checkpoints.start(1, "session two")

    assert checkpoints.restore_points() == [(1, "session two", [])]
    checkpoints.activate("test-session", resume=True)
    assert checkpoints.restore_points() == [(1, "session one", [])]


def test_only_recent_100_checkpoints_are_retained():
    for turn in range(1, 102):
        checkpoints.start(turn, f"turn {turn}", events=turn - 1)

    points = checkpoints.restore_points()
    assert len(points) == checkpoints.MAX_CHECKPOINTS
    assert points[0][0] == 2
    assert points[-1][0] == 101

    checkpoints.activate("test-session", resume=True)
    assert [turn for turn, _, _ in checkpoints.restore_points()] == list(
        range(2, 102)
    )


def test_corrupt_checkpoint_index_fails_loudly():
    checkpoints.start(1, "saved")
    index = Path(".minicc/checkpoints/test-session/index.json")
    index.write_text("{broken", encoding="utf-8")
    checkpoints.activate("fallback-session")
    checkpoints.start(1, "fallback")

    with pytest.raises(checkpoints.CheckpointCorruptError, match="is corrupt"):
        checkpoints.activate("test-session", resume=True)
    assert checkpoints.restore_points() == [(1, "fallback", [])]


def test_checkpoint_index_rejects_too_many_records():
    root = Path(".minicc/checkpoints/oversized-session")
    root.mkdir(parents=True)
    records = [
        {"turn": turn, "query": str(turn), "events": turn, "files": {}}
        for turn in range(1, checkpoints.MAX_CHECKPOINTS + 2)
    ]
    (root / "index.json").write_text(
        json.dumps({"version": 1, "checkpoints": records}),
        encoding="utf-8",
    )

    with pytest.raises(
        checkpoints.CheckpointCorruptError,
        match="invalid checkpoint list",
    ):
        checkpoints.activate("oversized-session", resume=True)


def test_checkpoint_index_rejects_duplicate_backup_ids():
    root = Path(".minicc/checkpoints/duplicate-session")
    root.mkdir(parents=True)
    records = [
        {
            "turn": 1,
            "query": "duplicate",
            "events": 0,
            "files": {"a.txt": "0", "b.txt": "0"},
        }
    ]
    (root / "index.json").write_text(
        json.dumps({"version": 1, "checkpoints": records}),
        encoding="utf-8",
    )

    with pytest.raises(checkpoints.CheckpointCorruptError, match="is invalid"):
        checkpoints.activate("duplicate-session", resume=True)


def test_start_rolls_back_memory_when_index_write_fails(monkeypatch):
    def fail_save():
        raise checkpoints.CheckpointError("disk full")

    monkeypatch.setattr(checkpoints, "_save_index", fail_save)

    with pytest.raises(checkpoints.CheckpointError, match="disk full"):
        checkpoints.start(1, "not persisted")

    assert checkpoints.restore_points() == []


def test_restore_keeps_checkpoint_if_index_commit_fails(monkeypatch):
    target = Path("atomic.txt")
    target.write_text("before")
    checkpoints.start(1, "edit")
    checkpoints.before_write(target)
    target.write_text("after")
    backup = Path(".minicc/checkpoints/test-session/1/0")
    def fail_save():
        raise checkpoints.CheckpointError("disk full")

    monkeypatch.setattr(checkpoints, "_save_index", fail_save)

    with pytest.raises(checkpoints.CheckpointError, match="disk full"):
        checkpoints.restore_files(1)

    assert target.read_text() == "before"
    assert checkpoints.restore_points() == [(1, "edit", ["atomic.txt"])]
    assert backup.exists()


def test_committed_restore_ignores_backup_cleanup_failure(monkeypatch):
    target = Path("cleanup.txt")
    target.write_text("before")
    checkpoints.start(1, "edit")
    checkpoints.before_write(target)
    target.write_text("after")

    def fail_cleanup(_path):
        raise OSError("busy")

    monkeypatch.setattr(checkpoints, "_rmtree", fail_cleanup)

    assert checkpoints.restore_files(1) == (1, [])
    assert target.read_text() == "before"
    assert checkpoints.restore_points() == []


def test_hook_fires_in_real_agent_loop(monkeypatch):
    """Drive the actual agent_loop (model response mocked) through a real
    write_file call, and confirm the checkpoint hook captured it for /rewind."""
    from minicc import query_engine as engine, permissions

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
    monkeypatch.setattr(engine, "llm_response", lambda *a, **k: next(responses))

    from minicc.tools import freshness

    freshness.record("target.py")  # satisfy read-before-overwrite (see test_freshness)
    history = [{"role": "user", "content": "rewrite target"}]
    checkpoints.start(1, "rewrite target")
    engine.agent_loop(history)

    assert Path("target.py").read_text() == "REWRITTEN"        # agent actually wrote it
    assert checkpoints.restore_files(1) == (1, [])             # hook captured it
    assert Path("target.py").read_text() == "ORIGINAL"         # /rewind restores
    permissions.reset()


def test_checkpoint_failure_blocks_write_without_crashing(monkeypatch):
    from minicc import query_engine as engine

    class Block:
        id = "tool-1"
        name = "write_file"
        input = {"path": "blocked.txt", "content": "new"}

    def fail_checkpoint(_path):
        raise checkpoints.CheckpointIOError("disk full")

    monkeypatch.setattr(checkpoints, "before_write", fail_checkpoint)
    monkeypatch.setattr(engine, "confirm", lambda *args, **kwargs: True)

    output = engine._run_tool(
        Block(),
        {"write_file"},
        session_id=None,
        indent="",
    )

    assert "could not create a rewind checkpoint" in output
    assert "file was not modified" in output
    assert not Path("blocked.txt").exists()


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
    cp = checkpoints._checkpoints[-1]
    backup = (
        Path(".minicc/checkpoints/test-session/1") / cp["files"]["y.txt"]
    )
    backup.unlink()                                      # corrupt: backup vanishes
    assert checkpoints.restore_files(1) == (0, ["y.txt"])
    assert checkpoints.restore_points() == [(1, "t", ["y.txt"])]
