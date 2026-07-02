"""Session persistence: an append-only transcript per session in .minicc/sessions/.

Each session is a JSONL file (`<id>.jsonl`), written one event per line and NEVER
rewritten — so the raw conversation survives even when the in-memory working set is
compacted (an overwrite-on-save scheme would drop the summarized history). Two event kinds:

    {"t": "msg",     "m": <one API-shaped message>}   # appended as the turn happens
    {"t": "compact", "state": [<messages>]}           # post-compaction working set

`load` replays the log: a `msg` event appends; a `compact` event RESETS the working
set to its recorded state (summary + kept tail). So reconstruction yields exactly
what the live session held — small and API-ready — no matter how long the raw log
grew, while the pre-compaction `msg` events stay on disk (lossless).

Serialization: assistant messages hold SDK Block objects (TextBlock/ToolUseBlock)
that don't JSON-serialize; `model_dump(exclude_none=True)` yields minimal, API-clean
dicts (dropping SDK-only fields like `caller`/`citations`). Strings and existing
dicts pass through unchanged.
"""

import json
from datetime import datetime
from pathlib import Path

_SESSIONS_SUBDIR = ".minicc/sessions"


def _dir() -> Path:
    return Path.cwd() / _SESSIONS_SUBDIR


def _path(session_id: str) -> Path:
    return _dir() / f"{session_id}.jsonl"


def new_id() -> str:
    """A timestamp-based session id, e.g. 20260616_143022."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _serialize_message(m) -> dict:
    """One message → a JSON-serializable, API-clean dict.

    SDK Block objects → model_dump(exclude_none=True); strings and existing dicts
    pass through. Fails loud on an unknown block type rather than saving a dead
    repr that can't round-trip back to the API.
    """
    content = m.get("content")
    if isinstance(content, list):
        blocks = []
        for b in content:
            if isinstance(b, dict):
                blocks.append(b)
            elif hasattr(b, "model_dump"):
                blocks.append(b.model_dump(exclude_none=True))
            else:
                raise TypeError(
                    f"un-serializable block in message content: {type(b).__name__}"
                )
        return {"role": m["role"], "content": blocks}
    return {"role": m["role"], "content": content}


def _serialize_messages(messages) -> list:
    """A list of messages → JSON-serializable dicts (see _serialize_message)."""
    return [_serialize_message(m) for m in messages]


def _ensure_dir() -> Path:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    # self-ignoring .minicc/ so session files never get git-tracked
    gitignore = d.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    return d


def _append_event(session_id: str, event: dict) -> None:
    _ensure_dir()
    with _path(session_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_message(session_id: str, message) -> None:
    """Append one message to the transcript (append-only, in conversation order)."""
    _append_event(session_id, {"t": "msg", "m": _serialize_message(message)})


def log_compaction(session_id: str, working_set) -> None:
    """Record a compaction: the post-compaction working set (summary + kept tail).

    On load this RESETS the reconstructed history to this state, so the raw `msg`
    events before it stay on disk (lossless) without re-inflating the working set.
    """
    _append_event(
        session_id, {"t": "compact", "state": _serialize_messages(working_set)}
    )


def latest_id() -> str | None:
    """Most recently modified session id in this cwd, or None."""
    d = _dir()
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def load(session_id: str) -> list | None:
    """Replay the transcript into the working set (dict-form, API-ready). None if
    the session doesn't exist. `msg` appends; `compact` resets to its state."""
    path = _path(session_id)
    if not path.exists():
        return None
    working: list = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("t") == "msg":
                working.append(event["m"])
            elif event.get("t") == "compact":
                working = list(event["state"])
    except (json.JSONDecodeError, OSError):
        return None
    return working
