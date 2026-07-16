"""File-freshness registry — CC's read-before-edit contract.

CC's Edit/Write tools enforce two invariants (their exact error strings,
observed live in CC sessions): a file must have been Read before it may be
edited, and the edit is rejected if the file changed on disk after that read
("modified, either by the user or by a linter"). Together they stop the agent
from clobbering content it has never seen — the harness-level half of the
"file state is current in your context" mechanism CC advertises in its Edit
results. minicc mirrors the contract with a session-scoped path→mtime
registry: read_file records, edit_file/write_file check before writing and
re-record after, so consecutive edits by the agent itself stay free while any
external change (user, linter, bash) forces a re-read.
"""

from pathlib import Path

_SEEN: dict[str, float] = {}  # resolved path → mtime at last read/agent-write


def _key(path) -> str | None:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return None


def record(path) -> None:
    """Mark a file as freshly seen (called after read_file and after our own
    writes, so the agent's own edits never trip the staleness check)."""
    k = _key(path)
    if k is None:
        return
    try:
        _SEEN[k] = Path(k).stat().st_mtime
    except OSError:
        pass


def check(path) -> str | None:
    """None if the file may be written, else an actionable Error string.
    Wording mirrors CC's own errors so the recovery behavior transfers."""
    k = _key(path)
    if k is None:
        return None
    if k not in _SEEN:
        return (
            "Error: File has not been read yet. Read it first with read_file "
            "before editing it."
        )
    try:
        if Path(k).stat().st_mtime != _SEEN[k]:
            return (
                "Error: File has been modified since it was last read — possibly "
                "by the user or a linter. Read it again with read_file before "
                "attempting to write it."
            )
    except OSError:
        pass
    return None


def reset() -> None:
    """Forget everything (new session / /clear)."""
    _SEEN.clear()
