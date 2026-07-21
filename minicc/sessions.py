"""Session persistence: an append-only transcript per session in .minicc/sessions/.

Each session is a JSONL file (`<id>.jsonl`), written one event per line and NEVER
rewritten — so the raw conversation survives even when the in-memory working set is
compacted (an overwrite-on-save scheme would drop the summarized history). Three event kinds:

    {"t": "msg",     "ts": <iso>, "m": <one API-shaped message>[, "usage": {...}]}
    {"t": "compact", "state": [<messages>]}           # post-compaction working set
    {"t": "rewind",  "state": [<messages>]}           # conversation /rewind reset

`ts` (every msg) and `usage` (assistant msgs, token counts from the API response)
mirror CC's transcript fields — they're what make post-hoc process analysis
(timing, cost, turn economy) possible. Replay ignores unknown keys, so old
transcripts without them stay loadable.

`load` replays the log: a `msg` event appends; a `compact` or `rewind` event RESETS
the working set to its recorded state (summary + kept tail). So reconstruction yields exactly
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

from minicc import config

_SESSIONS_SUBDIR = ".minicc/sessions"


def _dir() -> Path:
    return Path.cwd() / _SESSIONS_SUBDIR


def _path(session_id: str) -> Path:
    return _dir() / f"{session_id}.jsonl"


def path(session_id: str | None) -> Path | None:
    """Public transcript path for a session (None if no session). Hooks put this
    in their stdin payload as `transcript_path`, matching CC."""
    return _path(session_id) if session_id else None


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


def _append_event(session_id: str, event: dict) -> None:
    config.ensure_project_dir("sessions")
    with _path(session_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_message(session_id: str, message, usage=None, meta=False) -> None:
    """Append one message to the transcript (append-only, in conversation order).
    `usage` (assistant messages): the API response's usage object; recorded so
    process analysis gets per-turn token counts for free. `meta` marks a
    harness-generated expansion (a slash command's rendered content) — CC's
    transcripts mark these `isMeta: true` as the second record of a two-record
    pair (tags message + expansion message; probed live 2026-07-17). The flag
    rides the transcript RECORD only, never the API message."""
    event = {
        "t": "msg",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "m": _serialize_message(message),
    }
    if meta:
        event["meta"] = True
    if usage is not None:
        event["usage"] = {
            k: getattr(usage, k, 0) or 0
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        }
    _append_event(session_id, event)


def log_compaction(session_id: str, working_set) -> None:
    """Record a compaction: the post-compaction working set (summary + kept tail).

    On load this RESETS the reconstructed history to this state, so the raw `msg`
    events before it stay on disk (lossless) without re-inflating the working set.
    """
    _append_event(
        session_id, {"t": "compact", "state": _serialize_messages(working_set)}
    )


def log_rewind(session_id: str, working_set) -> None:
    """Record a conversation rewind: the post-rewind working set. Same reset
    semantics as `compact` on replay — the transcript stays append-only, so the
    rewound-away messages remain on disk (lossless)."""
    _append_event(
        session_id, {"t": "rewind", "state": _serialize_messages(working_set)}
    )


def event_count(session_id: str) -> int:
    """Number of events currently in the transcript (0 if none). Recorded by
    checkpoints at turn start so a conversation rewind can replay back to it."""
    path = _path(session_id)
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())


def latest_id() -> str | None:
    """Most recently modified session id in this cwd, or None."""
    d = _dir()
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def _replay(lines, upto: int | None = None) -> list:
    """Replay transcript events into a working set. `msg` appends; `compact` and
    `rewind` RESET to their recorded state. `upto` limits to the first N events."""
    working: list = []
    seen = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if upto is not None and seen >= upto:
            break
        seen += 1
        event = json.loads(line)
        if event.get("t") == "msg":
            working.append(event["m"])
        elif event.get("t") in ("compact", "rewind"):
            working = list(event["state"])
    return _repair_dangling_tool_uses(working)


def _repair_dangling_tool_uses(msgs: list) -> list:
    """Make a replayed working set API-valid: every tool_use must be answered
    by a tool_result in the NEXT message, or the request 400s. Transcripts
    written before the max_tokens truncation fix can carry an unanswered
    partial tool_use (dogfood R5: output cap hit mid tool-call); repair by
    inserting/merging a synthetic result rather than refusing to resume."""
    out: list = []
    for i, m in enumerate(msgs):
        out.append(m)
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        ids = [
            b.get("id")
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        if not ids:
            continue
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        answered = set()
        next_has_results = (
            nxt is not None
            and nxt.get("role") == "user"
            and isinstance(nxt.get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in nxt["content"]
            )
        )
        if next_has_results:
            answered = {
                b.get("tool_use_id")
                for b in nxt["content"]
                if isinstance(b, dict) and b.get("type") == "tool_result"
            }
        missing = [t for t in ids if t not in answered]
        if not missing:
            continue
        synthetic = [
            {
                "type": "tool_result",
                "tool_use_id": t,
                "content": "[interrupted: no result was recorded]",
            }
            for t in missing
        ]
        if next_has_results:
            # partial answers exist: ALL results must live in that one next
            # message, so merge the synthetic ones into it
            nxt["content"] = list(nxt["content"]) + synthetic
        else:
            out.append({"role": "user", "content": synthetic})
    return out


def _load(session_id: str, upto: int | None) -> list | None:
    """Replay the transcript into a working set (dict-form, API-ready), optionally
    stopping after the first `upto` events. None if the session doesn't exist or
    the file is unreadable/corrupt."""
    path = _path(session_id)
    if not path.exists():
        return None
    try:
        return _replay(path.read_text(encoding="utf-8").splitlines(), upto=upto)
    except (json.JSONDecodeError, OSError):
        return None


def load(session_id: str) -> list | None:
    """The full transcript replayed into the working set (--continue/--resume)."""
    return _load(session_id, upto=None)


def load_upto(session_id: str, n_events: int) -> list | None:
    """The working set as of the first `n_events` transcript events — the state a
    conversation rewind restores to. Replaying (not slicing the live history)
    means it works across compaction boundaries."""
    return _load(session_id, upto=n_events)
