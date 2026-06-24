"""Session persistence: save/load the conversation to .minicc/sessions/.

The one tricky part is serialization — assistant messages hold SDK Block objects
(TextBlock/ToolUseBlock) that don't JSON-serialize. We convert them with
`model_dump(exclude_none=True)`, which yields minimal API-clean dicts (dropping
SDK-only fields like `caller`/`citations`). See SESSIONS.md.
"""

import json
import time
from datetime import datetime
from pathlib import Path

_SESSIONS_SUBDIR = ".minicc/sessions"


def _dir() -> Path:
    return Path.cwd() / _SESSIONS_SUBDIR


def new_id() -> str:
    """A timestamp-based session id, e.g. 20260616_143022."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _serialize_messages(messages) -> list:
    """Make `messages` JSON-serializable.

    SDK Block objects (assistant content) → model_dump(exclude_none=True) to get
    minimal, API-clean dicts. Strings and existing dicts pass through unchanged.
    """
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            blocks = []
            for b in content:
                if isinstance(b, dict):
                    blocks.append(b)            # tool_result blocks minicc builds
                elif hasattr(b, "model_dump"):
                    blocks.append(b.model_dump(exclude_none=True))  # SDK Block objects
                else:
                    # Shouldn't happen: content holds only dicts or SDK blocks.
                    # Fail loud — str(b) would silently save a dead repr that
                    # can't round-trip back to the API, corrupting the session.
                    raise TypeError(
                        f"un-serializable block in message content: {type(b).__name__}"
                    )
            out.append({"role": m["role"], "content": blocks})
        else:
            out.append({"role": m["role"], "content": content})
    return out


def save(session_id: str, messages, model: str) -> Path:
    """Write the session to .minicc/sessions/<id>.json (overwriting)."""
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    # self-ignoring .minicc/ so session files never get git-tracked
    gitignore = d.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")

    path = d / f"{session_id}.json"
    data = {
        "id": session_id,
        "saved": time.time(),
        "model": model,
        "cwd": str(Path.cwd()),
        "messages": _serialize_messages(messages),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


def latest_id() -> str | None:
    """Most recently modified session id in this cwd, or None."""
    d = _dir()
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def load(session_id: str) -> list | None:
    """Load a session's messages (dict-form, API-ready). None if not found."""
    path = _dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("messages", [])
