"""Task store — CC's Task* system (the coordination substrate, not a todo list).

CC's task system is a stateful, id-keyed store with a dependency graph, not the
stateless plan `todo_write` rendered. It's the substrate the coordinator/teams
tiers stand on — self-claim (`owner`), `blocks`/`blockedBy`, auto-unblock — see
docs/CC_AGENTS_COORDINATOR_DESIGN.md. Phase-1 decision D1 replaces todo_write
with it. Background-tier tools (TaskOutput/TaskStop) are deferred; this ships
the four coordination tools: create / get / list / update.

This is the COORDINATOR task-LIST (pending/in_progress/completed + owner +
dep graph), which is a DIFFERENT layer from CC's runtime task REGISTRY (the
7-TaskType / 5-TaskStatus running/failed/killed model + isTerminalTaskStatus
behind TaskOutput/TaskStop/Monitor). The registry is the background/agent
execution tracker — deferred to build steps 3–4; the 3-status choice here is
deliberate, not incomplete. See design doc §2b (unify-vs-two-layer is an open
call for the background tier).

Storage: `.minicc/tasks/<id>.json`, project-scoped and PERSISTENT — CC's task
dir "persists locally … resumed sessions keep their tasks." One file per task
keeps per-task locking natural for the later teams tier; a single lock file
guards id allocation + dependency mutations (fcntl on unix; a no-op elsewhere).

Schema mirrors CC's Task* tools verbatim: id, subject, description, activeForm,
status (pending→in_progress→completed, or `deleted`=remove), owner (claim by
setting it), blocks/blockedBy (symmetric graph), metadata. A task is claimable
when `pending` AND every blockedBy task is completed — **auto-unblock is
emergent** (completing a blocker frees its dependents; computed at read time,
no cross-file write on completion).
"""

import json
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-unix: locking degrades to a no-op (darwin/linux have it)
    fcntl = None

STATUSES = ("pending", "in_progress", "completed")
_MARK = {"completed": "✓", "in_progress": "▶", "pending": "☐"}


def _dir() -> Path:
    d = Path.cwd() / ".minicc" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    gi = d.parent / ".gitignore"  # keep .minicc/ self-ignoring (sessions precedent)
    if not gi.exists():
        gi.write_text("*\n")
    return d


@contextmanager
def _lock():
    """Serialize id allocation + dependency mutations across processes
    (teams-ready). Not reentrant — hold it once around a read-modify-write."""
    lockf = open(_dir() / ".lock", "w")
    try:
        if fcntl is not None:
            fcntl.flock(lockf, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def _path(task_id: str) -> Path:
    return _dir() / f"{task_id}.json"


def _read(task_id: str) -> dict | None:
    try:
        return json.loads(_path(task_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write(rec: dict) -> None:
    _path(rec["id"]).write_text(json.dumps(rec, ensure_ascii=False, indent=2))


def _all() -> list[dict]:
    out = []
    for f in _dir().glob("*.json"):  # ".lock" has no .json suffix — excluded
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda r: int(r["id"]))


def _next_id() -> str:
    return str(max((int(r["id"]) for r in _all()), default=0) + 1)


# ─── mutations ────────────────────────────────────────────────────────────────

def create(subject: str, description: str = "", active_form=None, metadata=None) -> dict:
    with _lock():
        rec = {
            "id": _next_id(),
            "subject": subject,
            "description": description or "",
            "activeForm": active_form or "",
            "status": "pending",
            "owner": "",
            "blocks": [],
            "blockedBy": [],
            "metadata": metadata or {},
        }
        _write(rec)
    return rec


def update(task_id: str, *, status=None, subject=None, description=None,
           active_form=None, owner=None, metadata=None,
           add_blocks=None, add_blocked_by=None) -> dict | None:
    with _lock():
        rec = _read(task_id)
        if rec is None:
            return None
        if status == "deleted":
            _delete(rec)
            return {"id": task_id, "status": "deleted"}
        if status is not None:
            rec["status"] = status
        if subject is not None:
            rec["subject"] = subject
        if description is not None:
            rec["description"] = description
        if active_form is not None:
            rec["activeForm"] = active_form
        if owner is not None:
            rec["owner"] = owner
        for k, v in (metadata or {}).items():  # merge; null deletes a key (CC)
            if v is None:
                rec["metadata"].pop(k, None)
            else:
                rec["metadata"][k] = v
        for dep in add_blocks or []:
            _link(rec, str(dep), "blocks")       # rec blocks dep → dep blockedBy rec
        for dep in add_blocked_by or []:
            _link(rec, str(dep), "blockedBy")     # rec blockedBy dep → dep blocks rec
        _write(rec)
        return rec


def _link(rec: dict, other_id: str, direction: str) -> None:
    """Keep the graph symmetric. A dangling dep id is skipped silently."""
    other = _read(other_id)
    if other is None or other_id == rec["id"]:
        return
    inverse = "blockedBy" if direction == "blocks" else "blocks"
    if other_id not in rec[direction]:
        rec[direction].append(other_id)
    if rec["id"] not in other[inverse]:
        other[inverse].append(rec["id"])
        _write(other)


def _delete(rec: dict) -> None:
    for other_id in rec.get("blocks", []) + rec.get("blockedBy", []):
        other = _read(other_id)
        if not other:
            continue
        for field in ("blocks", "blockedBy"):
            if rec["id"] in other.get(field, []):
                other[field].remove(rec["id"])
        _write(other)
    try:
        _path(rec["id"]).unlink()
    except OSError:
        pass


# ─── reads ────────────────────────────────────────────────────────────────────

def get(task_id: str) -> dict | None:
    return _read(task_id)


def list_all() -> list[dict]:
    return _all()


def open_blockers(rec: dict) -> list[str]:
    """The blockedBy ids not yet completed — CC's TaskList shows exactly these.
    A task with an empty result here is claimable (this filter IS auto-unblock)."""
    out = []
    for bid in rec.get("blockedBy", []):
        b = _read(bid)
        if b is not None and b.get("status") != "completed":
            out.append(bid)
    return out


# ─── rendering (tool_result strings) ──────────────────────────────────────────

def render_list() -> str:
    tasks = _all()
    if not tasks:
        return "tasks: (none)"
    lines = []
    for t in tasks:
        suffix = f"  owner={t['owner']}" if t.get("owner") else ""
        blk = open_blockers(t)
        if blk:
            suffix += f"  blockedBy={','.join('#' + b for b in blk)}"
        lines.append(f"{_MARK.get(t['status'], '?')} #{t['id']} {t['subject']}{suffix}")
    return "tasks:\n" + "\n".join(lines)


def render_detail(rec: dict) -> str:
    blocks = ",".join("#" + b for b in rec.get("blocks", [])) or "-"
    blocked = ",".join("#" + b for b in rec.get("blockedBy", [])) or "-"
    return (
        f"#{rec['id']} [{rec['status']}] {rec['subject']}\n"
        f"  description: {rec['description'] or '-'}\n"
        f"  owner: {rec['owner'] or '-'}\n"
        f"  blocks: {blocks}   blockedBy: {blocked}"
        + (f"\n  metadata: {json.dumps(rec['metadata'], ensure_ascii=False)}"
           if rec.get("metadata") else "")
    )
