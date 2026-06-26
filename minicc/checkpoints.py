"""File checkpoint / rewind — back up files before the agent edits them, so
`/rewind N` can restore them to an earlier turn. See CHECKPOINT.md.

Design: per-file copy (D1), per-turn (D2), code-only (D3 — files revert, the
conversation is kept), backups on disk (D4) so memory stays flat. Only
write_file/edit_file are tracked; bash-made changes are not (PERMISSIONS.md).
"""

from pathlib import Path

ABSENT = None  # sentinel: file did not exist at checkpoint time → delete on rewind

_DIR_NAME = ".minicc/checkpoints"
_stack = []  # [{turn, query, dir: Path, files: {path: backup_id | ABSENT}}]


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


def start(turn: int, query: str):
    """Open a checkpoint for a new turn. The turn dir is created lazily on the
    first backup, so read-only turns cost nothing."""
    _stack.append({"turn": turn, "query": query, "dir": None, "files": {}})


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
        cp["dir"] = _root() / str(cp["turn"])
        cp["dir"].mkdir(parents=True, exist_ok=True)
        gi = _root().parent / ".gitignore"  # keep .minicc/ self-ignoring
        if not gi.exists():
            gi.write_text("*\n")
    backup_id = str(len(cp["files"]))
    (cp["dir"] / backup_id).write_bytes(p.read_bytes())
    cp["files"][path] = backup_id


def restore_points():
    """Turns that changed files, oldest→newest: [(turn, query), ...] for /rewind."""
    return [(cp["turn"], cp["query"]) for cp in _stack if cp["files"]]


def restore_files(turn: int):
    """Revert files to their state before `turn`. Restores every checkpoint from
    the top down to and including `turn` (newest-first so the oldest backup wins),
    then discards them. Returns `(restored_count, failed_paths)`, or None if `turn`
    isn't a restore point. A per-file error (e.g. its parent dir was removed by a
    bash command, or a backup file is missing) is collected in `failed_paths`
    rather than aborting the whole rewind and leaving a half-restored tree."""
    idx = next((i for i, cp in enumerate(_stack) if cp["turn"] == turn and cp["files"]), None)
    if idx is None:
        return None
    restored, failed = 0, []
    for cp in reversed(_stack[idx:]):
        for path, backup_id in cp["files"].items():
            try:
                if backup_id is ABSENT:
                    Path(path).unlink(missing_ok=True)
                else:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)   # dir may be gone
                    Path(path).write_bytes((cp["dir"] / backup_id).read_bytes())
                restored += 1
            except OSError:
                failed.append(path)
        if cp["dir"] is not None:
            _rmtree(cp["dir"])
    del _stack[idx:]
    return restored, failed
