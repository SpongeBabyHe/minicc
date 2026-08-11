# CHECKPOINT.md — file-snapshot checkpoint / rewind

`/rewind` undoes the agent's file changes back to an earlier turn. Code (built):
`minicc/checkpoints.py`, with the write boundary in `query_engine.py` and commands
in `minicc/cli/commands.py`.

## Decisions

- **D1 capture = per-file copy** (not a git-tree snapshot). Before
  `write_file`/`edit_file` mutates a file, back up its current bytes (or mark
  `ABSENT` if new). **Trade-off: bash-made changes are NOT tracked** — minicc
  can't know what files a shell command touched (see [PERMISSIONS.md](PERMISSIONS.md):
  bash is unbounded). Same limitation as CC. Documented, not hidden.
- **D2 granularity = per user turn.** A checkpoint per turn — "rewind to before
  I asked X". **Every** turn is a restore point (CC: one checkpoint per prompt);
  the `/rewind` list marks which files each turn changed. Each checkpoint also
  records the session transcript's event count at turn start — the replay anchor
  for a conversation rewind.
- **D3 scope = three modes, code by default.** `/rewind N [code|conversation|both]`:
  - **`code`** (default) reverts **files**, keeps the **conversation** — CC's
    praised "keep what was tried, reset the files" mode. Never truncates `history`,
    so it can't orphan a `tool_use`; a short notice tells the model.
  - **`conversation`** replays the append-only transcript back to just before turn
    N's prompt (files kept). Replaying — not slicing the live history — means it
    works **across compaction boundaries**: the raw pre-compaction messages are
    still on disk (SESSIONS.md). The rewind itself is an appended reset event, so
    the transcript stays lossless; the rewound-away turns remain recoverable.
    The stale context-size read is reset so the next turn doesn't spuriously compact.
  - **`both`** does files + conversation.
  (Conversation truncation can't 400: the replayed state is a turn-start boundary,
  where every `tool_use` already has its `tool_result` — the same invariant the
  interrupt-safe transcript recording maintains.)
- **D4 storage = per session on disk** under
  `.minicc/checkpoints/<session-id>/` (self-ignored, like `bash_outputs`).
  Backup **content** lives in per-turn directories and the small index
  (`turn → query/events/files`) is atomically persisted as `index.json`.
  Resume reloads that index; `/clear` activates a new session directory without
  deleting the old session's checkpoints. Only the most recent 100 restore
  points are retained, matching CC's documented checkpoint window.
- **D5 UX = `/rewind`** lists restore points as a contiguous numbered list
  `[1..N]` (every user turn); `/rewind N` reverts to point N. The index
  is *not* the internal turn number — turn numbers have gaps from read-only turns,
  which is confusing as a restore id (caught in the first live test).

## Data model

In memory, an ordered list of JSON-native checkpoints (one per user turn):

```
Checkpoint = {
  turn,
  query,
  events,
  files: {path: backup_id | ABSENT},
}
```

This list is exactly the value persisted as `index.json["checkpoints"]`;
turn-directory paths are derived from `session-id + turn` when needed. File keys
are canonical project-relative paths (absolute only for targets outside the
project), so aliases such as `a.txt` and `./a.txt` cannot create two snapshots of
the same file in one turn.

On disk:

```
.minicc/checkpoints/<session-id>/
├── index.json
└── <turn>/<backup_id>
```

`<backup_id>` holds the original bytes of one backed-up file. `ABSENT` in the
index means the file didn't exist at checkpoint time and is deleted on rewind.

## Algorithm

- **activate(session_id, resume)** — startup and `/clear`: bind the checkpoint
  module to one session; resume validates and reloads `index.json`. Activation is
  transactional: a corrupt or unreadable target index leaves the previously
  active session untouched.
- **start(turn, query, events)** — cli, at each turn start: append and persist a
  checkpoint. The turn dir is created lazily on the first byte backup, so
  read-only turns need only a small index record.
- **before_write(path)** — query_engine.py, before running
  `write_file`/`edit_file` (and only once the write is approved): canonicalize the
  target and, if it hasn't already been captured this turn, stream its current
  bytes to disk (or record `ABSENT`). Non-regular paths and calls without an
  active checkpoint are ignored. If persistence fails, the write is not run and
  the model receives a tool error instead of losing rewind safety.
- **restore_files(n)** — cli `/rewind N [code|both]`: for checkpoints from the top
  down to and including turn n, restore each one's files **newest-first** (so turn
  n's original — the oldest — wins for a file touched in several turns): write the
  bytes back, or delete if `ABSENT`. Only after every restore succeeds are those
  checkpoints removed from the index; any failure retains their metadata and
  backups so the user can fix the cause and retry. Unreferenced backup-directory
  cleanup is best-effort after the index commit. `n` may be a read-only turn —
  then only later turns' files revert. In `code` mode the turn counter is **not**
  reset and `history` is **not** truncated; a notice is appended:
  `[Files were rewound to an earlier checkpoint; edits since then are undone.]`
  Returns `(restored_count, failed_paths)`: restore recreates a missing parent dir
  (it may have been `rm`'d by bash) and collects per-file errors into
  `failed_paths` instead of aborting half-way. The cli surfaces N is a contiguous
  restore-point index (the /rewind list), not the internal turn number.
- **conversation mode** — cli reads the checkpoint's `events` anchor, rebuilds the
  working set via `sessions.load_upto(session_id, events)`, replaces `history`
  in place, logs a `rewind` reset event to the transcript, and resets the cached
  context size (`ctx.reset_size()`).

Newest-first is correct: a file edited in turns n and n+2 → applying n+2's backup
then n's leaves n's (pre-turn-n) content.

## Limitations

- **bash changes**: not tracked (D1). Surfaced in `/rewind` output.
- **directory create/move/delete**: not undone (file content only — as in CC).
- **non-regular files**: directories, sockets, devices, and FIFOs are not
  checkpointed.
- **deep rewind (code mode)**: reverting to a much earlier turn leaves later
  (now-undone) turns in the conversation — correct but noisy; the notice keeps the
  model right, and `/rewind N both` (or `/compact`) tidies it.

## Deferred

- Per-tool-call granularity; a git-tree mode that would also cover bash.
- Session-age cleanup coordinated with transcript retention.
