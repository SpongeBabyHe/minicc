"""File checkpoint / rewind — snapshot a file's bytes before the agent first
edits it in a turn, so `/rewind N` can undo back to an earlier turn. See
CHECKPOINT.md.

The model (one checkpoint per turn, like CC's one-checkpoint-per-prompt):

- `_stack` holds one checkpoint per turn in memory. Within a turn a file is
  backed up ONCE, on first write (later edits in the same turn restore to the
  turn's starting state, not to an intermediate one).
- Backup BYTES live on disk (`.minicc/checkpoints/<turn>/`), not in memory, so
  the stack stays flat however large the files are. The turn's dir is created
  lazily on the first backup — a read-only turn costs nothing.
- Only write_file/edit_file are tracked; bash-made changes are invisible to it
  (PERMISSIONS.md), so a rewind can't undo them.
- A file that did NOT exist at checkpoint time is recorded as `ABSENT` and
  "restored" by deletion.

Two rewind flavors ride on this:

- CODE: `restore_files(turn)` reverts every checkpoint from the top down to and
  including `turn` (newest-first, so the oldest backup wins), then discards them.
- CONVERSATION: each checkpoint also records the transcript event count at turn
  start (`events`); `/rewind N conversation|both` replays the transcript back to
  that point via sessions.load_upto — which works across compaction boundaries
  (see SESSIONS.md) because it replays the log rather than slicing live history.
"""

from pathlib import Path

from minicc import config

ABSENT = None  # sentinel: file did not exist at checkpoint time → delete on rewind

_DIR_NAME = ".minicc/checkpoints"
# [{turn, query, events, dir: Path, files: {path: backup_id | ABSENT}}] per turn
_stack = []


def _root() -> Path:
    return Path.cwd() / _DIR_NAME


def reset():
    """Drop all checkpoints (memory + disk). Called by /clear and at startup."""
    global _stack
    _stack = []
    root = _root()
    if root.exists():
        for turn_dir in root.glob("*"):
            _rmtree(turn_dir)


def _rmtree(p: Path):
    if p.is_dir():
        for child in p.iterdir():
            _rmtree(child)
        p.rmdir()
    else:
        p.unlink(missing_ok=True)


def start(turn: int, query: str, events: int = 0):
    """Open a checkpoint for a new turn. `events` = the transcript event count at
    turn start (BEFORE this turn's user message), the replay target for a
    conversation rewind. The turn dir is created lazily on the first backup, so
    read-only turns cost nothing."""
    _stack.append(
        {"turn": turn, "query": query, "events": events, "dir": None, "files": {}}
    )


def before_write(path):
    """Back up `path`'s current bytes before it's modified — once per checkpoint.
    No-op if no checkpoint is active (e.g. a read-only sub-agent)."""
    if not _stack or not path:
        return
    cp = _stack[-1]
    if path in cp["files"]:
        return
    p = Path(path)
    if not p.exists():
        cp["files"][path] = ABSENT
        return
    if cp["dir"] is None:
        cp["dir"] = config.ensure_project_dir(f"checkpoints/{cp['turn']}")
    backup_id = str(len(cp["files"]))
    (cp["dir"] / backup_id).write_bytes(p.read_bytes())
    cp["files"][path] = backup_id


def restore_points():
    """Every turn, oldest→newest: [(turn, query, changed_paths), ...] for /rewind.
    All turns are restore points (CC: one checkpoint per prompt); `changed_paths`
    shows which files that turn touched (empty for read-only turns)."""
    return [(cp["turn"], cp["query"], list(cp["files"])) for cp in _stack]


def events_at(turn: int) -> int | None:
    """The transcript event count recorded at `turn`'s start (the conversation-
    rewind replay target), or None if the turn isn't on the stack."""
    return next((cp["events"] for cp in _stack if cp["turn"] == turn), None)


def restore_files(turn: int):
    """Revert files to their state before `turn`. Restores every checkpoint from
    the top down to and including `turn` (newest-first so the oldest backup wins),
    then discards them. Returns `(restored_count, failed_paths)`, or None if `turn`
    isn't a restore point. `turn` may be a read-only turn — then only LATER turns'
    files revert (restored_count can be 0). A per-file error (e.g. its parent dir
    was removed by a bash command, or a backup file is missing) is collected in
    `failed_paths` rather than aborting the whole rewind and leaving a
    half-restored tree."""
    idx = next((i for i, cp in enumerate(_stack) if cp["turn"] == turn), None)
    if idx is None:
        return None
    restored, failed = 0, []
    for cp in reversed(_stack[idx:]):
        for path, backup_id in cp["files"].items():
            try:
                if backup_id is ABSENT:
                    Path(path).unlink(missing_ok=True)
                else:
                    Path(path).parent.mkdir(parents=True,
                                            exist_ok=True)   # dir may be gone
                    Path(path).write_bytes(
                        (cp["dir"] / backup_id).read_bytes())
                restored += 1
            except OSError:
                failed.append(path)
        if cp["dir"] is not None:
            _rmtree(cp["dir"])
    del _stack[idx:]
    return restored, failed
