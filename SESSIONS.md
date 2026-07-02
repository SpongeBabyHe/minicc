# Session persistence (Tier 1) — focused note

Save the conversation to disk so a session can be resumed after quitting (or a
crash). Daily-use value is high (don't lose a long working session). Two
non-obvious decisions: **(1)** serializing the messages list (below), and **(2)**
an **append-only transcript** so in-session compaction doesn't lose history on disk
(see "Append-only transcript").

## The one real decision: serializing `history` for a safe round-trip

The whole feature is one round-trip:

```
record:  each message ──serialize──► appended to a JSONL transcript on disk
resume:  JSONL ──replay──► working set ──► client.messages.create(messages=...)
```

The hard part is the last arrow: the loaded data is sent **back to the API**, so
it must be a valid request. Everything below is in service of "resume doesn't
400."

### Why `history` can't just be `json.dumps`'d

`history` is heterogeneous:

| message | `content` type | JSON-native? |
|---|---|---|
| user query | `str` | ✅ |
| assistant | `list[SDK Block]` (`TextBlock`/`ToolUseBlock`, Pydantic) | ❌ |
| tool_result | `list[dict]` | ✅ |

`json.dumps(ToolUseBlock(...))` → `TypeError: not JSON serializable`. So the SDK
blocks must be converted. Two options:

- `str(block)` → `"ToolUseBlock(id=...)"`, a **repr string**. Loads back as a
  dead string — you can't feed it to the API as a tool_use. ❌
- `block.model_dump(...)` → a structured **dict** of the block's fields. JSON-
  native, and round-trips back into a real content block. ✅

So `model_dump` is the only option that survives the round-trip.

### What the API actually accepts (the input schema)

This is the "API safety" target. From the SDK's input `*Param` TypedDicts
(`Required[...]` = mandatory):

| Block | required | optional |
|---|---|---|
| message | `role`, `content` | — |
| text | `type`, `text` | `cache_control`, `citations` |
| tool_use | `type`, `id`, `name`, `input` | `cache_control`, `caller` |
| tool_result | `type`, `tool_use_id` | `content`, `is_error`, `cache_control` |

"API-safe" means the loaded data matches this schema on **two** levels:

- **(A) field shape** — each block has its required fields and no field with an
  invalid value.
- **(B) structure** — every `tool_use` is followed by its matching
  `tool_result`, and roles alternate. (This is the rule we hit live: a stray
  `tool_use` with no `tool_result` → *"tool_use ids were found without
  tool_result blocks"* 400.)

### Why `exclude_none=True` (and not plain `model_dump()`)

Plain `model_dump()` keeps optional SDK fields set to `None`:

```
ToolUseBlock.model_dump()              → {type, id, name, input, caller: None}
ToolUseBlock.model_dump(exclude_none=True) → {type, id, name, input}      # caller dropped
TextBlock.model_dump(exclude_none=True)    → {type, text}                 # citations dropped
```

`exclude_none=True` yields **exactly the required-field set** — the minimal valid
form. That guarantees level (A): required fields present, zero optional fields,
so nothing can have an invalid value.

> **Corrected from an earlier draft.** I first said keeping `caller: None` risks
> an "unexpected field" rejection. The schema above shows that's imprecise:
> `caller`/`citations` **are** recognized optional input fields, not unknown
> ones. The real (still-untested) risk is that they're *typed* (`caller: Caller`,
> not nullable), so sending `None` as their value is of uncertain validity — the
> API might reject it or ignore it. `exclude_none=True` is safe **regardless**,
> because it doesn't emit any optional field at all. That's the actual reason to
> prefer it: it sidesteps the question instead of betting on the answer.

### Why structure (B) survives too

`_serialize_messages` walks messages and converts blocks **1:1, in order** — it
never drops, adds, or reorders. So a `tool_use` and its following `tool_result`
stay adjacent and paired, and roles stay alternating. The serialization can't
create the orphaned-tool_use 400.

### The resumed history is *mixed* — and that's fine

On resume, old messages are dicts (from `model_dump`); new turns append SDK
objects (fresh `response.content`). The API accepts dict-form content, and
minicc's helpers were already built to handle **both** dicts and SDK objects
(`_serialize_for_summary`, `_estimate_tokens`, `_evict_old_tool_result`). So a
mixed history works everywhere.

### Confidence (honest)

- **Verified**: `exclude_none` produces the required-field shape; it JSON
  round-trips; a 14-message session **including a `tool_use` turn** resumed via
  `--continue` with no 400 (live test). So the chosen design works end-to-end.
- **Untested**: whether plain `model_dump()` (with `caller:None`) would 400.
  Not claimed — `exclude_none` removes the need to know.

## Append-only transcript (compaction stays lossless)

Storage is a **JSONL event log** (`<id>.jsonl`), one event per line, never
rewritten. Two event kinds: `{"t":"msg","m":<message>}` and
`{"t":"compact","state":[...]}`. `load` replays: a `msg` appends; a `compact`
RESETS the working set to its recorded state (summary + kept tail).

**Why not overwrite a single JSON each turn (the old design)?** In-session L4
compaction rewrites `history` in place (summary + recent), so overwriting on save
persisted the *compacted* history — the raw pre-compaction messages were silently
lost. An append-only log fixes this: the raw `msg` events stay on disk (line count
only grows), while the `compact` event lets a resume reconstruct the small working
set instead of re-inflating the whole raw log. Mirrors Claude Code's JSONL
transcript + `compact_boundary`. See docs/CC_ALIGNMENT_PLAN.md (item A).

**Recording points** (`session_id` threaded `agent_loop → llm_response → _compact`;
sub-agents pass `None`, so their isolated context is never recorded):
- the user message — appended before the turn runs;
- assistant + its tool_results — appended **together, only after both exist**, so a
  Ctrl-C mid-tool never persists a dangling `tool_use` (which would 400 on resume);
- a `compact` event — written from inside `_compact`, in conversation order.

## Routine decisions (no design depth)

- **Storage**: `.minicc/sessions/<id>.jsonl` in the cwd (append-only; see above).
  Reuses the self-ignoring `.minicc/` dir convention. One file per session;
  per-project by construction (sessions live under the project's cwd).
- **Session id**: startup timestamp, e.g. `20260616_143022`.
- **Recording**: incremental, as the turn happens (no turn-end overwrite). A
  crash/quit loses at most the in-flight turn; the interrupt-safe recording order
  means the transcript never ends on a dangling `tool_use`.
- **Resume interface**: CLI flags (resume must happen at startup, before the
  loop — a flag is more natural than a slash command):
  - `--continue` → resume the most recent session in this cwd.
  - `--resume <id>` → resume a specific session.
  - no flag → fresh session.
- **On resume**: load history, reload CLAUDE.md fresh (same as startup), continue.

## Scope
- ✅ serialize + append-only transcript + `--continue` + `--resume <id>`
- ✅ `/clear` rotates to a new session id — the pre-clear transcript stays on disk;
  new turns record to a fresh `<id>.jsonl`.
- ⬜ session listing UI / picker — defer (just `--continue` for the common case)
- ⬜ conversation-level `/rewind` to a boundary — the transcript now makes this
  possible (raw events on disk); not yet wired.

## Status
✅ Implemented. serialize + append-only transcript + `--continue`/`--resume`;
`/clear` rotates sessions; compaction records a boundary so a resume reconstructs
`[summary]+tail` losslessly. Unit tests cover the serialize round-trip, boundary
reconstruction, and append-only losslessness.
