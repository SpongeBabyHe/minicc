# Session persistence (Tier 1) — focused note

Save the conversation to disk so a session can be resumed after quitting (or a
crash). Daily-use value is high (don't lose a long working session); demo value
is low — so this is a focused note, not a full design doc. It has exactly **one**
non-obvious decision: serializing the messages list.

## The one real decision: serializing `history` for a safe round-trip

The whole feature is one round-trip:

```
save:    history ──serialize──► JSON file on disk
resume:  JSON file ──load──► history ──► client.messages.create(messages=history)
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

## Routine decisions (no design depth)

- **Storage**: `.minicc/sessions/<id>.json` in the cwd. Reuses the self-ignoring
  `.minicc/` dir convention. One file per session; per-project by construction
  (sessions live under the project's cwd).
- **Session id**: startup timestamp, e.g. `20260616_143022`.
- **Auto-save**: after each *successful* turn (overwrite the session file). A
  crash/quit loses at most the in-flight turn. Interrupted turns are rolled back
  (cli.py `del history[mark:]`), so the saved state is always API-valid.
- **Resume interface**: CLI flags (resume must happen at startup, before the
  loop — a flag is more natural than a slash command):
  - `--continue` → resume the most recent session in this cwd.
  - `--resume <id>` → resume a specific session.
  - no flag → fresh session.
- **On resume**: load history, reload CLAUDE.md fresh (same as startup), continue.

## MVP scope (for the deadline)
- ✅ serialize/save/load + `--continue` + `--resume <id>`
- ⬜ session listing UI / picker — defer (just `--continue` for the common case)
- ⬜ `/clear` rotating to a new session id — for MVP `/clear` just empties the
  current session; the pre-clear history is overwritten on next save. (Known
  simplification.)

## Status
✅ Implemented (Tier 1). serialize/save/load + `--continue`/`--resume`; 6 unit
tests for the round-trip; live-tested via `--continue` (14-message session incl.
a tool_use turn resumed without a 400).
