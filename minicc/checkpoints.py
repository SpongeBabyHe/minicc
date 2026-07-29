"""Per-session file checkpoints for ``/rewind``.

Each user prompt creates one restore point. The small checkpoint index is stored
at ``.minicc/checkpoints/<session-id>/index.json`` and file bytes are copied
lazily into sibling ``<turn>/`` directories before the first write in that turn.
Both metadata and backups therefore survive process exit and session resume.

Only ``write_file`` and ``edit_file`` participate; shell-made changes remain
outside the checkpoint contract. Conversation rewind anchors are transcript event
counts and are replayed by :mod:`minicc.sessions`.
"""

import json
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TypedDict

from minicc import config, sessions

ABSENT: None = None
MAX_CHECKPOINTS = 100

_DIR_NAME = ".minicc/checkpoints"
_INDEX_NAME = "index.json"
_INDEX_VERSION = 1
_BACKUP_ID = re.compile(r"^[0-9]+$")


class _Checkpoint(TypedDict):
    turn: int
    query: str
    events: int
    files: dict[str, str | None]


# JSON-native records; this list is exactly index.json["checkpoints"].
_checkpoints: list[_Checkpoint] = []
_session_id: str | None = None


class CheckpointError(Exception):
    """Base class for checkpoint lifecycle or persistence failures."""


class CheckpointCorruptError(CheckpointError):
    """A persisted checkpoint index cannot be trusted."""


class CheckpointIOError(CheckpointError, OSError):
    """Checkpoint state could not be read, written, or removed."""


def _root() -> Path:
    """Return the project checkpoint root without creating it."""
    return Path.cwd() / _DIR_NAME


def _session_root(session_id: str | None = None) -> Path:
    """Return one session's checkpoint directory."""
    selected = session_id if session_id is not None else _session_id
    if selected is None:
        raise CheckpointError("no checkpoint session is active")
    return _root() / sessions.validate_id(selected)


def _index_path(session_id: str | None = None) -> Path:
    return _session_root(session_id) / _INDEX_NAME


def _checkpoint_dir(turn: int) -> Path:
    return _session_root() / str(turn)


def _canonical_path(path: str | Path) -> tuple[Path, str]:
    """Return the actual target and its stable project-relative identity."""
    try:
        target = Path(path).expanduser().resolve(strict=False)
        project = Path.cwd().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise CheckpointError(f"invalid checkpoint path {path!r}: {error}") from error
    try:
        stored = str(target.relative_to(project))
    except ValueError:
        stored = str(target)
    return target, stored


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    """Remove a tree without following a directory symlink."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    for child in path.iterdir():
        _rmtree(child)
    path.rmdir()


def _cleanup_tree(path: Path) -> None:
    """Best-effort removal for data already made unreachable by an index commit."""
    try:
        _rmtree(path)
    except OSError:
        pass


def _save_index() -> None:
    """Atomically persist the active in-memory checkpoint index."""
    if _session_id is None:
        raise CheckpointError("no checkpoint session is active")
    temporary: Path | None = None
    try:
        root = config.ensure_project_dir(f"checkpoints/{_session_id}")
        serialized = json.dumps(
            {
                "version": _INDEX_VERSION,
                "checkpoints": _checkpoints,
            },
            ensure_ascii=False,
            indent=2,
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=f".{_INDEX_NAME}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
        index = root / _INDEX_NAME
        temporary.replace(index)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        _unlink_quietly(temporary)
        raise CheckpointIOError(
            f"could not persist checkpoints for session {_session_id!r}: {error}"
        ) from error


def _load_index(session_id: str) -> list[_Checkpoint]:
    """Load and validate one session's checkpoint index."""
    index = _index_path(session_id)
    try:
        raw = index.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as error:
        raise CheckpointCorruptError(
            f"checkpoint index for session {session_id!r} is corrupt: {error}"
        ) from error
    except OSError as error:
        raise CheckpointIOError(
            f"could not read checkpoints for session {session_id!r}: {error}"
        ) from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CheckpointCorruptError(
            f"checkpoint index for session {session_id!r} is corrupt: {error}"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _INDEX_VERSION
    ):
        raise CheckpointCorruptError(
            f"checkpoint index for session {session_id!r} has an unsupported shape"
        )
    records = payload.get("checkpoints")
    if not isinstance(records, list) or len(records) > MAX_CHECKPOINTS:
        raise CheckpointCorruptError(
            f"checkpoint index for session {session_id!r} has an invalid checkpoint list"
        )

    loaded: list[_Checkpoint] = []
    previous_turn = 0
    previous_events = -1
    for position, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise CheckpointCorruptError(
                f"checkpoint {position} for session {session_id!r} is not an object"
            )
        turn = record.get("turn")
        query = record.get("query")
        events = record.get("events")
        files = record.get("files")
        valid_turn = (
            isinstance(turn, int)
            and not isinstance(turn, bool)
            and turn > previous_turn
        )
        valid_events = (
            isinstance(events, int)
            and not isinstance(events, bool)
            and events >= 0
            and events >= previous_events
        )
        normalized_files: dict[str, str | None] = {}
        backup_ids: set[str] = set()
        valid_files = isinstance(files, dict)
        if valid_files:
            for path, backup_id in files.items():
                if not isinstance(path, str) or not path:
                    valid_files = False
                    break
                try:
                    _, normalized = _canonical_path(path)
                except CheckpointError:
                    valid_files = False
                    break
                if normalized in normalized_files:
                    valid_files = False
                    break
                if backup_id is not ABSENT:
                    if (
                        not isinstance(backup_id, str)
                        or not _BACKUP_ID.fullmatch(backup_id)
                        or backup_id in backup_ids
                    ):
                        valid_files = False
                        break
                    backup_ids.add(backup_id)
                normalized_files[normalized] = backup_id
        if (
            not valid_turn
            or not isinstance(query, str)
            or not valid_events
            or not valid_files
        ):
            raise CheckpointCorruptError(
                f"checkpoint {position} for session {session_id!r} is invalid"
            )
        previous_turn = turn
        previous_events = events
        loaded.append(
            {
                "turn": turn,
                "query": query,
                "events": events,
                "files": normalized_files,
            }
        )
    return loaded


def activate(session_id: str, *, resume: bool = False) -> None:
    """Bind checkpoints to ``session_id``, loading its index when resuming."""
    global _session_id, _checkpoints
    selected = sessions.validate_id(session_id)
    if resume:
        checkpoints = _load_index(selected)
    else:
        try:
            _rmtree(_session_root(selected))
        except OSError as error:
            raise CheckpointIOError(
                f"could not clear checkpoints for session {selected!r}: {error}"
            ) from error
        checkpoints = []

    _session_id = selected
    _checkpoints = checkpoints


def last_turn() -> int:
    """Return the most recent checkpoint turn, or zero for a fresh session."""
    return _checkpoints[-1]["turn"] if _checkpoints else 0


def start(turn: int, query: str, events: int = 0) -> None:
    """Persist a restore point before one user turn begins."""
    if _session_id is None:
        raise CheckpointError("activate a session before starting checkpoints")
    if (
        not isinstance(turn, int)
        or isinstance(turn, bool)
        or turn <= last_turn()
    ):
        raise ValueError(
            "checkpoint turns must be positive and strictly increasing")
    if not isinstance(query, str):
        raise TypeError("checkpoint query must be a string")
    if (
        not isinstance(events, int)
        or isinstance(events, bool)
        or events < 0
        or (
            _checkpoints
            and events < _checkpoints[-1]["events"]
        )
    ):
        raise ValueError(
            "checkpoint event counts must be non-negative and non-decreasing")

    previous = list(_checkpoints)
    expired: list[_Checkpoint] = []
    try:
        _checkpoints.append(
            {
                "turn": turn,
                "query": query,
                "events": events,
                "files": {},
            }
        )
        while len(_checkpoints) > MAX_CHECKPOINTS:
            expired.append(_checkpoints.pop(0))
        _save_index()
    except BaseException:
        _checkpoints[:] = previous
        raise
    for checkpoint in expired:
        _cleanup_tree(_checkpoint_dir(checkpoint["turn"]))


def _next_backup_id(
    checkpoint: _Checkpoint,
    checkpoint_dir: Path,
) -> str:
    used = {
        backup_id
        for backup_id in checkpoint["files"].values()
        if backup_id is not ABSENT
    }
    candidate = 0
    while True:
        backup_id = str(candidate)
        backup = checkpoint_dir / backup_id
        if (
            backup_id not in used
            and not backup.exists()
            and not backup.is_symlink()
        ):
            return backup_id
        candidate += 1


def before_write(path: str | Path | None) -> None:
    """Back up ``path`` once in the active turn before it is modified."""
    if not _checkpoints or path is None or path == "":
        return
    checkpoint = _checkpoints[-1]
    target, path_text = _canonical_path(path)
    if path_text in checkpoint["files"]:
        return

    try:
        target_mode = target.stat().st_mode
    except FileNotFoundError:
        checkpoint["files"][path_text] = ABSENT
        try:
            _save_index()
        except BaseException:
            checkpoint["files"].pop(path_text, None)
            raise
        return
    except OSError as error:
        raise CheckpointIOError(
            f"could not inspect {path_text!r} before writing: {error}"
        ) from error

    if not stat.S_ISREG(target_mode):
        return

    checkpoint_dir = _checkpoint_dir(checkpoint["turn"])
    backup_id = _next_backup_id(checkpoint, checkpoint_dir)
    backup = checkpoint_dir / backup_id
    try:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, backup)
    except OSError as error:
        _unlink_quietly(backup)
        try:
            checkpoint_dir.rmdir()
        except OSError:
            pass
        raise CheckpointIOError(
            f"could not back up {path_text!r}: {error}"
        ) from error

    checkpoint["files"][path_text] = backup_id
    try:
        _save_index()
    except BaseException:
        checkpoint["files"].pop(path_text, None)
        _unlink_quietly(backup)
        try:
            checkpoint_dir.rmdir()
        except OSError:
            pass
        raise


def restore_points() -> list[tuple[int, str, list[str]]]:
    """Return every restore point from oldest to newest."""
    return [
        (checkpoint["turn"], checkpoint["query"], list(checkpoint["files"]))
        for checkpoint in _checkpoints
    ]


def events_at(turn: int) -> int | None:
    """Return the transcript event anchor recorded before ``turn``."""
    return next(
        (
            checkpoint["events"]
            for checkpoint in _checkpoints
            if checkpoint["turn"] == turn
        ),
        None,
    )


def restore_files(turn: int) -> tuple[int, list[str]] | None:
    """Restore from ``turn`` onward, retaining recovery data after any failure."""
    index = next(
        (
            position
            for position, checkpoint in enumerate(_checkpoints)
            if checkpoint["turn"] == turn
        ),
        None,
    )
    if index is None:
        return None

    restored = 0
    failed: list[str] = []
    failed_paths: set[str] = set()
    removed = _checkpoints[index:]
    for checkpoint in reversed(removed):
        for path, backup_id in checkpoint["files"].items():
            try:
                target = Path(path)
                if backup_id is ABSENT:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    backup = _checkpoint_dir(checkpoint["turn"]) / backup_id
                    if backup.is_symlink() or not backup.is_file():
                        raise FileNotFoundError(
                            f"checkpoint backup is missing: {backup}"
                        )
                    shutil.copyfile(backup, target)
                restored += 1
            except OSError:
                if path not in failed_paths:
                    failed_paths.add(path)
                    failed.append(path)

    if failed:
        return restored, failed

    del _checkpoints[index:]
    try:
        _save_index()
    except BaseException:
        _checkpoints[index:index] = removed
        raise
    for checkpoint in removed:
        _cleanup_tree(_checkpoint_dir(checkpoint["turn"]))
    return restored, failed
