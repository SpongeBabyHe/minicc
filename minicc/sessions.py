"""Session persistence: an append-only transcript per session in .minicc/sessions/.

Each session is a JSONL file (`<id>.jsonl`), written one event per line and NEVER
rewritten — so the raw conversation survives even when the in-memory working set is
compacted (an overwrite-on-save scheme would drop the summarized history). Four event kinds:

    {"t": "msg",     "ts": <iso>, "m": <one API-shaped message>[, "usage": {...}]}
    {"t": "compact", "state": [<messages>]}           # post-compaction working set
    {"t": "rewind",  "state": [<messages>]}           # conversation /rewind reset
    {"t": "context_edit", "edits": [<tool-result replacements>]}

`ts` (every msg) and `usage` (assistant msgs, token counts from the API response)
mirror CC's transcript fields — they're what make post-hoc process analysis
(timing, cost, turn economy) possible. Replay ignores unknown keys, so old
transcripts without them stay loadable.

`load` replays the log: a `msg` event appends; a `compact` or `rewind` event RESETS
the working set to its recorded state (summary + kept tail); a `context_edit`
event replaces selected old tool-result contents. So reconstruction yields
exactly what the live session held — small and API-ready — no matter how long the
raw log grew, while the original `msg` events stay on disk (lossless).

Serialization: assistant messages hold SDK Block objects (TextBlock/ToolUseBlock)
that don't JSON-serialize; `model_dump(exclude_none=True)` yields minimal, API-clean
dicts (dropping SDK-only fields like `caller`/`citations`). Strings and existing
dicts pass through unchanged.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from minicc import config

_SESSIONS_SUBDIR = ".minicc/sessions"
_TOOL_OUTPUTS_SUBDIR = ".minicc/tool_outputs"


def _dir() -> Path:
    """Return the sessions directory for the current working directory."""
    return Path.cwd() / _SESSIONS_SUBDIR


def _path(session_id: str) -> Path:
    """Return the JSONL transcript path for ``session_id``.

    Args:
        session_id: Session identifier (file stem).

    Returns:
        Path to ``<session_id>.jsonl`` under the sessions directory.
    """
    return _dir() / f"{session_id}.jsonl"


def path(session_id: str | None) -> Path | None:
    """Return the public transcript path for a session, or None if unset.

    Hooks expose this as ``transcript_path`` in their stdin payload (CC parity).

    Args:
        session_id: Session id, or None when there is no session.

    Returns:
        Transcript path, or None when ``session_id`` is None.
    """
    return _path(session_id) if session_id else None


def _safe_component(value: str, fallback: str) -> str:
    """Make an opaque id safe for use as one path component."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe[:48] or fallback


def tool_result_output_path(
    session_id: str,
    tool_use_id: str,
    content,
) -> Path:
    """Return the deterministic spill path for one evicted tool result."""
    safe_session = _safe_component(session_id, "session")
    safe_tool = _safe_component(tool_use_id, "tool-result")
    digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:10]
    suffix = ".txt" if isinstance(content, str) else ".json"
    return (
        Path.cwd()
        / _TOOL_OUTPUTS_SUBDIR
        / safe_session
        / f"{safe_tool}-{digest}{suffix}"
    )


def persisted_tool_result_marker(output_path: Path) -> str:
    """Build the in-context pointer left after a tool result is spilled."""
    return (
        "<persisted-output>\n"
        f"Full tool result saved to: {output_path}\n"
        "Use read_file on this path to inspect the full result.\n"
        "</persisted-output>"
    )


def persist_tool_result_output(
    session_id: str,
    tool_use_id: str,
    content,
) -> Path:
    """Persist an evicted result outside active context and return its path."""
    config.ensure_project_dir("tool_outputs")
    output_path = tool_result_output_path(session_id, tool_use_id, content)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        serialized = content
    else:
        serialized = json.dumps(
            content,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    output_path.write_text(serialized, encoding="utf-8")
    return output_path


def new_id() -> str:
    """Allocate a timestamp-based session id.

    Returns:
        Id such as ``20260616_143022``.
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _serialize_message(m) -> dict:
    """Convert one message into a JSON-serializable, API-clean dict.

    SDK Block objects become ``model_dump(exclude_none=True)`` dicts; plain
    strings and existing dicts pass through. Unknown block types raise so a
    non-round-trippable repr is never written.

    Args:
        m: Message dict with ``role`` and ``content``.

    Returns:
        Dict with ``role`` and serialized ``content``.

    Raises:
        TypeError: A content block is neither a dict nor an SDK model.
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
    """Serialize a list of messages for transcript storage.

    Args:
        messages: Iterable of message dicts.

    Returns:
        List of JSON-serializable message dicts (see ``_serialize_message``).
    """
    return [_serialize_message(m) for m in messages]


def _append_event(session_id: str, event: dict) -> None:
    """Append one JSON event line to the session transcript.

    Args:
        session_id: Target session id.
        event: Event dict to serialize as one JSONL line.
    """
    config.ensure_project_dir("sessions")
    with _path(session_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_message(session_id: str, message, usage=None, meta=False) -> None:
    """Append one conversation message to the transcript.

    Writes an append-only ``msg`` event in conversation order. ``usage`` and
    ``meta`` ride the transcript record only — never the API message body.

    Args:
        session_id: Target session id.
        message: API-shaped message (may contain SDK Block objects).
        usage: Optional API usage object (assistant turns). Records
            ``input_tokens``, ``output_tokens``, ``cache_read_input_tokens``,
            and ``cache_creation_input_tokens`` for cost analysis.
        meta: If True, mark a harness expansion (slash-command rendered
            content). Matches CC's ``isMeta`` on the second record of a
            tags + expansion pair.
    """
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
    """Record a compaction boundary with the post-compaction working set.

    On load, a ``compact`` event resets reconstructed history to this state.
    Earlier ``msg`` events remain on disk (lossless) but do not re-inflate the
    working set.

    Args:
        session_id: Target session id.
        working_set: Messages after compaction (summary + kept tail).
    """
    _append_event(
        session_id, {"t": "compact", "state": _serialize_messages(working_set)}
    )


def log_context_edit(session_id: str, replacements) -> None:
    """Record tool-result replacements without copying the full working set.

    The original result remains in its earlier ``msg`` event. Replay applies this
    delta so resume matches the live working set, while replaying to a point
    before the edit restores the original result.
    """
    edits = [
        {
            "tool_use_id": replacement["tool_use_id"],
            "content": replacement["content"],
        }
        for replacement in replacements
    ]
    if edits:
        _append_event(session_id, {"t": "context_edit", "edits": edits})


def log_rewind(session_id: str, working_set) -> None:
    """Record a conversation rewind with the post-rewind working set.

    Replay resets to this state like ``compact``. Rewound-away messages stay
    on disk because the transcript is append-only.

    Args:
        session_id: Target session id.
        working_set: Messages after the rewind.
    """
    _append_event(
        session_id, {"t": "rewind", "state": _serialize_messages(working_set)}
    )


def event_count(session_id: str) -> int:
    """Count events currently in the transcript.

    Checkpoints record this at turn start so a conversation rewind can replay
    back to that position.

    Args:
        session_id: Target session id.

    Returns:
        Number of non-empty JSONL lines, or 0 if the file is missing.
    """
    path = _path(session_id)
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())


def latest_id() -> str | None:
    """Return the most recently modified session id in this cwd.

    Returns:
        Session id (file stem), or None if no transcripts exist.
    """
    d = _dir()
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def _replay(lines, upto: int | None = None) -> list:
    """Replay transcript event lines into a working-set message list.

    ``msg`` appends; ``compact`` and ``rewind`` replace the working set with
    their recorded ``state``; ``context_edit`` applies tool-result deltas.
    Always runs dangling-``tool_use`` repair on the result.

    Args:
        lines: Iterable of JSONL lines (possibly blank).
        upto: If set, stop after this many non-empty events.

    Returns:
        API-ready working-set messages (dict form).
    """
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
        elif event.get("t") == "context_edit":
            _apply_context_edit(working, event.get("edits", []))
    return _repair_dangling_tool_uses(working)


def _apply_context_edit(messages: list, edits: list) -> None:
    """Apply transcript tool-result replacements to a replayed working set."""
    replacements = {
        edit.get("tool_use_id"): edit.get("content")
        for edit in edits
        if edit.get("tool_use_id")
    }
    if not replacements:
        return
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if tool_use_id in replacements:
                block["content"] = replacements[tool_use_id]


def _repair_dangling_tool_uses(msgs: list) -> list:
    """Ensure every ``tool_use`` has a matching ``tool_result`` in the next message.

    The API 400s on unanswered ``tool_use`` ids. Older transcripts (e.g. mid
    ``tool_use`` cut by ``max_tokens``) may lack results; this inserts or merges
    synthetic ``tool_result`` blocks so resume can proceed.

    Args:
        msgs: Replayed working-set messages.

    Returns:
        A new list that is API-valid for tool-use pairing. May mutate the
        content list of an existing next user message when merging partial
        results.
    """
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
    """Load and replay a session transcript into a working set.

    Args:
        session_id: Session to load.
        upto: If set, replay only the first ``upto`` events; otherwise the
            full transcript.

    Returns:
        Dict-form, API-ready messages, or None if the session is missing or
        the file is unreadable/corrupt.
    """
    path = _path(session_id)
    if not path.exists():
        return None
    try:
        return _replay(path.read_text(encoding="utf-8").splitlines(), upto=upto)
    except (json.JSONDecodeError, OSError):
        return None


def load(session_id: str) -> list | None:
    """Load a session's full working set (``--continue`` / ``--resume``).

    Args:
        session_id: Session to load.

    Returns:
        Replayed working-set messages, or None if unavailable.
    """
    return _load(session_id, upto=None)


def load_upto(session_id: str, n_events: int) -> list | None:
    """Load the working set as of the first ``n_events`` transcript events.

    Used by conversation rewind. Replaying (not slicing live history) works
    across compaction boundaries.

    Args:
        session_id: Session to load.
        n_events: Number of leading transcript events to replay.

    Returns:
        Replayed working-set messages, or None if unavailable.
    """
    return _load(session_id, upto=n_events)
