# Session Persistence — Design

## 1. Purpose & scope

Durably persist a conversation so it survives process exit, crash, and in-session
context compaction — and can be resumed or rewound into a **valid API request**.

**Goals**
- Resume a prior session (`--continue` / `--resume`) exactly where it left off.
- Rewind the conversation to an earlier turn (`/rewind N conversation|both`).
- **Losslessness**: the raw conversation survives even after compaction discards
  it from the in-memory working set.
- Every reconstruction is a valid Messages API request (resume never 400s).
- Zero-cost fidelity to CC's transcript shape, so process analysis + tooling transfer.

**Non-goals**
- Session listing / picker UI (deferred; `--continue` covers the common case).
- Cross-machine sync / upload — transcripts are local, per-project.
- Secret redaction / encryption of the log.
- Sub-agent transcripts — their context is deliberately isolated (I5).

## 2. The model: two tiers

Two representations of a conversation exist; every decision follows from keeping
them correctly related.

| | Working set | Transcript |
|---|---|---|
| where | RAM (`history`) | disk (`<id>.jsonl`) |
| role | what's sent to the API each turn | durable source of truth |
| lifetime | ephemeral; lost on exit | permanent, append-only |
| mutation | rewritten in place by compaction | never rewritten |

The transcript is **not a serialization of the working set** — it's an event log
from which the working set is *replayed*. That indirection is what lets
losslessness and in-place compaction coexist (§4, I2).

## 3. Data model

One file per session: `.minicc/sessions/<id>.jsonl`, `<id>` = startup timestamp.
One JSON event per line, appended, never rewritten. Four event kinds:

```
{"t":"msg",     "ts":<iso>, "m":<message>[, "usage":{…}][, "meta":true]}
{"t":"compact", "state":[<messages>]}   # working set after a compaction
{"t":"rewind",  "state":[<messages>]}   # working set after a /rewind
{"t":"context_edit", "edits":[{"tool_use_id":<id>, "content":<replacement>}]}
```

- `m` — one API-shaped message (§5).
- `ts` / `usage` — timing + per-turn token counts (CC-parity fields); the
  substrate for offline process analysis. Never read on replay.
- `meta:true` — a slash-command expansion record (CC's `isMeta`); transcript-only.

Replay reads only `t`, `m`, `state`, and `edits`; **unknown keys are ignored** →
the format is forward/backward compatible.

## 4. Invariants (each → a mechanism → a test)

- **I1 — Replay yields a valid API request.**
  - *field shape*: required fields present, no invalid value → `model_dump(exclude_none=True)` (§5).
  - *structure*: every `tool_use` answered by a `tool_result` → two-layer defense (§6).
- **I2 — Losslessness.** Raw `msg` events are never deleted/rewritten. Compaction
  and local context editing append reset/delta events; original messages stay on
  disk forever.
- **I3 — Append-only.** Every write is `open("a")` + one line; no update-in-place.
- **I4 — Reminders never persist.** CLAUDE.md/memory/skills reminders are re-derived
  at request time; the transcript stores only the bare typed/expanded text.
- **I5 — Sub-agent context is never recorded.** Sub-agents run `session_id=None`.

## 5. Serialization: the round-trip contract

`history` is heterogeneous — user content `str`, tool_result `list[dict]`, but
assistant content `list[SDK Block]` (Pydantic; not JSON-native). Per block
(`_serialize_message`): dicts pass through; SDK blocks → `model_dump(exclude_none=True)`;
anything else raises (never persist a dead repr).

`exclude_none=True` emits **exactly the required-field set** — the minimal valid
form. It sidesteps whether a typed-but-`None` optional (e.g. `caller`) is legal
input by never emitting one. Generic across block types: a `thinking` block keeps
`{type, thinking, signature}`, so the replay-critical `signature` survives.

On resume the working set is **mixed** (old dicts + new SDK objects). Every helper
that walks history (`context_management.estimate_tokens`,
`context_management.evict_old_tool_results`, `llm._cacheable`) branches on
`isinstance`, so mixed content works everywhere.

## 6. Structural integrity: the dangling `tool_use`

The one structural rule that breaks in practice — a `tool_use` with no following
`tool_result` → 400. A turn can be cut mid-tool (Ctrl-C, `max_tokens`). Two layers:

- **Produce-time (prevent):** the assistant message and its `tool_result`s are
  recorded *together, only after both exist*; a `max_tokens` partial `tool_use` is
  discarded before recording. New transcripts never contain an orphan. This layer
  also keeps the *live* session valid (the load path never runs mid-session).
- **Load-time (repair):** `_replay` runs `_repair_dangling_tool_uses` — an
  unanswered `tool_use` gets a synthetic `tool_result` inserted/merged. Backstop
  for legacy transcripts or anything that slips through.

## 7. Interfaces

- **Record** (during a turn): `append_message` (user; then assistant+results together),
  `log_compaction`, `log_context_edit`, `log_rewind`. `session_id` threads
  `agent_loop → llm_response → compact`.
- **Read**: `load` (full replay — resume) · `load_upto(n)` (first n events — rewind) ·
  `event_count` (turn-start anchor) · `path` (transcript_path for hooks). `load`/
  `load_upto` share `_replay`; a missing/corrupt file → `None`.

## 8. Failure modes

- **Interrupt mid-turn** — memory rolled back; transcript keeps the partial turn.
  Replay: repair fixes any orphan; a lone user message merges (API allows
  consecutive same-role). Divergence is intentional (append-only lossless record).
- **Crash** — loses at most the in-flight line.
- **Corrupt trailing line** — `_load` returns `None` (whole-session load fails).
  *Debt: could tolerate a corrupt last line instead.*
- **Model boundary** — thinking blocks from a *different* model must drop on replay;
  minicc doesn't switch model mid-transcript, so untested. *Gap.*

## 9. CC fidelity & divergences

**Parity** (verified against a real CC transcript, 2026-07-24): per-session
append-only JSONL; per-message `timestamp` + `usage`; `isMeta` on slash-command
expansions; `tool_use` blocks inside message content; reminders re-derived at
request time, not persisted.

**Divergences** — minicc simplifies CC's transcript; the first two are
architectural, not cosmetic:

1. **Flat log vs tree.** CC's transcript is a parent-pointer DAG (`parentUuid` /
   `uuid` / `isSidechain` on every message) that can represent branches and
   sub-agent sidechains. minicc is a flat append-only list with `compact` /
   `rewind` resets and `context_edit` deltas — it cannot structurally represent a
   branch. (Upgrading to the tree is what rewind-as-branch or full teams replay
   would require.)
2. **Sub-agents unrecorded vs sidechains.** CC records sub-agent conversations in
   the transcript as sidechains (`isSidechain`). minicc deliberately does NOT record
   sub-agent context (I5) — a sub-agent runs `session_id=None`. A scope choice (keep
   sub-agents isolated; transcript = main line only), NOT parity.
3. **Compact shape.** CC flags the summary message with `isCompactSummary`; minicc
   writes a separate `{"t":"compact","state":[…]}` reset event.
4. **Event vocabulary.** CC's transcript also logs UI/harness events (titles, mode
   changes, attachments, queue operations, per-message `cwd`/`version`/`gitBranch`);
   minicc records only the conversation and working-set edits
   (`msg`/`compact`/`rewind`/`context_edit`).

Plus: session id is a startup timestamp, not CC's UUID; no `~/.claude` global store;
local-only (never uploaded).

## 10. Testing

Round-trip (14-msg incl. a tool_use turn resumes, no 400) · boundary reconstruction
(`[summary]`+tail) · append-only losslessness · dangling-repair (insert + merge paths).

## 11. Open / future

Session picker · corrupt-tail tolerance · secret redaction · teammate transcripts
(teams tier — per-process session ids + dirs).
