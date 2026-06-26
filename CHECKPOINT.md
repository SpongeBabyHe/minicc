# CHECKPOINT.md — file-snapshot checkpoint / rewind

`/rewind` undoes the agent's file changes back to an earlier turn. Code (built):
`minicc/checkpoints.py`, with a hook in `agent.py` and commands in `cli.py`.

## Decisions

- **D1 capture = per-file copy** (not a git-tree snapshot). Before
  `write_file`/`edit_file` mutates a file, back up its current bytes (or mark
  `ABSENT` if new). **Trade-off: bash-made changes are NOT tracked** — minicc
  can't know what files a shell command touched (see [PERMISSIONS.md](PERMISSIONS.md):
  bash is unbounded). Same limitation as CC. Documented, not hidden.
- **D2 granularity = per user turn.** A checkpoint per turn — "rewind to before
  I asked X". Only turns that actually changed files are restore points.
- **D3 scope = code-only.** `/rewind` reverts **files**, keeps the **conversation**.
  Reasons: (a) **safer** — it never truncates `history`, so it can't orphan a
  `tool_use` and 400 the next request; (b) the context-pruning that a
  "rewind conversation too" mode would add is already covered by `/compact`,
  `/clear`, and L3/L4; (c) it's CC's praised "keep what was tried, reset the
  files" mode. A short notice is appended so the model knows the revert happened.
- **D4 storage = on disk** under `.minicc/checkpoints/` (self-ignored, like
  `bash_outputs`). Backup **content** lives on disk → no memory bloat; the small
  index (turn → {path: backup}) lives in memory for the session. (Loading the
  index back after a restart = deferred; the data is already persisted.)
- **D5 UX = `/rewind`** lists restore points as a contiguous numbered list
  `[1..N]` (file-changing turns only); `/rewind N` reverts to point N. The index
  is *not* the internal turn number — turn numbers have gaps from read-only turns,
  which is confusing as a restore id (caught in the first live test).

## Data model

In memory, a stack of checkpoints (one per turn that changed files):

```
Checkpoint = {turn, query, dir: Path, files: {path: backup_id | ABSENT}}
```

On disk: `.minicc/checkpoints/<turn>/<backup_id>` holds the original bytes of one
backed-up file. `ABSENT` = the file didn't exist at checkpoint time → delete on
rewind.

## Algorithm

- **start(turn, query)** — cli, at each turn start: push an in-memory checkpoint
  (the turn dir is created lazily on the first backup, so read-only turns cost
  nothing).
- **before_write(path)** — agent.py, before running `write_file`/`edit_file` (and
  only once the write is approved): if `path` isn't already backed up this turn,
  copy its current bytes to disk (or record `ABSENT`). No-op if no checkpoint is
  active (sub-agents).
- **restore_files(n)** — cli `/rewind N`: for checkpoints from the top down to and
  including turn n, restore each one's files **newest-first** (so turn n's
  original — the oldest — wins for a file touched in several turns): write the
  bytes back, or delete if `ABSENT`. Then discard those checkpoints (rm their
  dirs). The turn counter is **not** reset and `history` is **not** truncated
  (code-only); instead a notice is appended:
  `[Files were rewound to an earlier checkpoint; edits since then are undone.]`
  Returns `(restored_count, failed_paths)`: restore recreates a missing parent dir
  (it may have been `rm`'d by bash) and collects per-file errors into
  `failed_paths` instead of aborting half-way. The cli surfaces N is a contiguous
  restore-point index (the /rewind list), not the internal turn number.

Newest-first is correct: a file edited in turns n and n+2 → applying n+2's backup
then n's leaves n's (pre-turn-n) content.

## Limitations

- **bash changes**: not tracked (D1). Surfaced in `/rewind` output.
- **directory create/move/delete**: not undone (file content only — as in CC).
- **deep rewind**: reverting to a much earlier turn leaves later (now-undone)
  turns in the conversation — correct but noisy; the notice keeps the model right,
  and `/compact` can tidy it.

## Deferred

- Load the checkpoint index after a restart (data is already on disk).
- A "rewind conversation too" mode (covered for now by `/compact` + `/clear`).
- Per-tool-call granularity; a git-tree mode that would also cover bash.
