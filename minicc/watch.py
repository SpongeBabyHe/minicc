"""watch — a minimal polling primitive for change detection.

minicc has no file-watcher infrastructure (and doesn't want the threads that
come with one); everything that needs "did X change?" polls instead. A Poller
wraps a snapshot function: `changed()` re-takes the snapshot and compares it
to the last one. Polling CADENCE is the caller's business — the REPL polls at
user-prompt boundaries (the only points where injection is possible anyway),
a future agent-teams loop can poll on its own schedule or from a thread.

The snapshot is any equality-comparable value. `mtime_snapshot` covers the
common case (a set of files: additions, deletions, and edits all change the
dict); a constant snapshot turns the poller into a fire-once latch (see
reminders.py, which watches skill files but deliberately NOT CLAUDE.md).
"""

from pathlib import Path


class Poller:
    """Remembers the last snapshot; `changed()` re-takes and compares.

    Starts unseeded, so the FIRST `changed()` always returns True — callers
    treat that as "initial load needed" (reminders.py relies on it)."""

    _UNSEEDED = object()

    def __init__(self, snapshot_fn):
        self._fn = snapshot_fn
        self._last = self._UNSEEDED

    def changed(self) -> bool:
        snap = self._fn()
        if snap != self._last:
            self._last = snap
            return True
        return False


def mtime_snapshot(paths) -> dict:
    """{path: mtime-or-None} for an iterable of paths. Missing files snapshot
    as None (so a file APPEARING is a change, same as one being edited)."""
    out = {}
    for p in paths:
        p = Path(p)
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:
            out[str(p)] = None
    return out
