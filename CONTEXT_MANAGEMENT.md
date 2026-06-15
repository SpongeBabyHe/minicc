# Context management

minicc's strategy for keeping the conversation history within Anthropic's
context window and rate limits. **Inspired by Claude Code's documented design,
not a 1:1 copy.**

## Background

LLM agents accumulate context turn by turn:
- Tool calls and their outputs (often kilobytes per call) go into history
- Every API request sends the **full** history
- Long sessions or large tool outputs cause:
  - **Cost**: input tokens scale linearly with conversation length
  - **Rate limits**: per-minute token quotas get hit
  - **Context window overflow**: hard cap at the model's window size

The strategy below uses a layered defense, mostly modeled on Claude Code's
publicly documented behavior (see [CC: How Claude Code works][cc-how] and
[CC: prompt caching][cc-cache]).

[cc-how]: https://code.claude.com/docs/en/how-claude-code-works
[cc-cache]: https://code.claude.com/docs/en/prompt-caching

## The 6 layers

| Layer | Name                             | Status        | What it does                                                                                                                                       |
| ----- | -------------------------------- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| L1    | 3-layer prompt cache + CLAUDE.md | ✅ Implemented | Cache stable prefixes (system / project / tools) so cached input tokens cost ~10%. Load CLAUDE.md as project-level memory that survives `/clear`.  |
| L2    | Tool output limits               | ✅ Implemented | Prevent a single tool call from blowing up context. Bash 30K + disk save; glob 100 cap; read_file truncation notice.                               |
| L3    | Tool_result eviction             | ✅ Implemented | When history exceeds threshold, blank out content of old `tool_result` messages (keep structure so model knows it called the tool). No LLM call.   |
| L4    | LLM auto-compact                 | ✅ Implemented | When eviction (L3) isn't enough, summarize older messages via an LLM call. Cut at an assistant boundary so tool_use/tool_result pairs stay intact. |
| L5    | Thrashing protection             | ✅ Implemented | Stop auto-compact after MAX_COMPACT_ATTEMPTS if a single message keeps refilling the context; raise instead of looping.                            |
| L6    | User controls                    | ✅ Implemented | `/context` ✅ (usage + eviction/compaction event counts). `/compact [focus]` and `/recap`                                                           |

Layers run in order: L1 reduces cost passively. L2 prevents single-call bloat.
L3 → L4 → L5 trigger sequentially when total context grows. L6 gives the user
visibility and manual control.

## Verified vs minicc's own choice

Where Claude Code's docs publish specifics, minicc follows them. Where docs
are silent, minicc makes its own engineering choice and labels it.

### Verified from Claude Code docs

- **Bash output cap**: 30,000 chars default, 150,000 hard ceiling. Beyond cap,
  save full output to file + return path + preview. (L2)
- **Glob match cap**: 100 results, sorted by mtime descending, with truncation
  flag. (L2)
- **Read pagination semantics**: `PARTIAL view` notice + `offset`/`limit`
  parameters. (L2, partial)
- **Auto-compact strategy**: clear older tool outputs first, then summarize
  conversation if needed. (L3 → L4 order)
- **Thrashing error**: literal message `Autocompact is thrashing: the context
  refilled to the limit...`. (L5)
- **Cache prefix layers**: System / Project context (CLAUDE.md) / Conversation.
  (L1)
- **CLAUDE.md size**: first 200 lines or 25KB, whichever first. (L1)
- **`/compact [focus]` argument**: user can steer what's preserved. (L6)
- **`/recap` doesn't mutate history**: cache-safe. (L6)

### minicc's own choices

- **read_file default limit**: not added — keep existing 50K char cap with
  added truncation notice. Defer line-based pagination + offset to v0.3 unless
  dogfood shows model needs it.
- **Token thresholds (L3 evict + L4 compact trigger)**: `TOKEN_BUDGET = 150_000`
  (single threshold; eviction runs first, compaction if still over). Estimated
  via `len(json.dumps(messages)) // 4`.
- **Tool_results kept intact during eviction**: `RECENT_TOOL_RESULTS_KEEP = 4`.
- **Messages kept verbatim after compaction**: `KEEP_RECENT_MESSAGES = 6` (cut
  lands on an assistant boundary at or after this point).
- **Summary input field cap**: `SUMMARY_FIELD_CAP = 1000` per field, so the
  summarization call can't itself balloon.
- **L4 compact summary prompt**: minicc's own template (Goal / Done / Key
  findings / In progress / Open questions) — CC's prompt is proprietary.
- **L5 thrashing retry limit**: `MAX_COMPACT_ATTEMPTS = 3` (CC says "several").
- **`.minicc/` self-ignoring directory pattern**: minicc's choice. Writes
  `.minicc/.gitignore: "*"` so artifacts never get tracked even if user forgets
  to gitignore `.minicc/` in their project.

## What minicc does NOT implement

Claude Code has these; minicc v0.2 doesn't. Deferred to v0.3+ based on dogfood
data.

| Feature                                       | Why CC has it                                          | Why minicc defers                                                                     |
| --------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Subagents (isolated context window)           | Token-heavy exploration without polluting main context | Significant architectural addition; will design based on observed need during dogfood |
| MEMORY.md (auto-written memory)               | Persistent learnings across sessions                   | Requires automatic writeback; design needs dogfood data                               |
| `/rewind` + file checkpoints                  | Roll back to earlier turn with snapshot restore        | Needs checkpoint store; not the primary pain in dogfood                               |
| Session persistence (`--resume`/`--continue`) | Continue conversations across CLI restarts             | minicc sessions are intentionally ephemeral; revisit if needed                        |

## Dogfood lessons & validation

Synthesized from dogfood on llm-kaki. Raw in-the-moment jots live in `PAIN.md`;
this section is the processed understanding.

### Validated
- **L1 cache works**: `cache_read` went 0 → 1465 across two turns (~41%
  cumulative hit rate). The 3 prefix breakpoints (system / project / tools)
  function as intended.
- **L1 CLAUDE.md shapes behavior**: asked to write path-handling code, the model
  used `pathlib` and noted "no os.path needed" *unprompted* — it absorbed
  CLAUDE.md's "never os.path" rule. Redundant re-reads of CLAUDE.md happen only
  on meta-questions ("what conventions?"), not during real work.
- **L3 eviction + re-read**: the model treats `EVICTED_MARKER` as "re-fetch if
  needed" and re-reads gracefully instead of confabulating — it even explained
  the mechanism when asked. Tradeoff: occasional extra re-reads; raise
  `RECENT_TOOL_RESULTS_KEEP` if they get frequent.
- **L4 + L5 (budget=3000 test)**: turn 1 fired compaction once and completed the
  task (L4 works); turn 2 hit a 16K-char single file > budget → 6 compaction
  attempts then thrash (L5 failsafe works). Both verified via `/context` event
  counters, not by hunting dim log lines.

### Fixed bugs
- **L4 cut-point**: cutting at *user-string* boundaries failed on a single long
  turn (only `msg[0]` qualifies → no cut → straight to thrash). Fixed by cutting
  before an *assistant* message — tool_use/tool_result pairs stay intact (the cut
  falls between pairs, never inside one). Works mid-turn, the common shape for a
  long single task.
- **Summary call balloon**: `_serialize_for_summary` now caps EVERY field
  (`SUMMARY_FIELD_CAP`), so a large write_file content can't make the
  summarization request itself huge and re-trip the rate limit.

### Design boundaries
- **Invariant**: `TOKEN_BUDGET` must exceed the max single tool output
  (read_file caps at 50K chars ≈ 12K tokens). Below that, one large file thrashes
  no matter what. At the 150K production budget this can't happen.
- **Eviction/compaction suit SEQUENTIAL tasks, not SURVEY tasks**: "read &
  report each, move on" coexists with eviction; "hold all files at once to
  answer" fights it (re-read churn → thrash). Don't set budget below the task's
  working set.
- **Compaction trades detail for space**: a follow-up needing summarized-away
  detail makes the model re-read. Inherent; mitigated by the `## In progress`
  section in the summary prompt and a production-sized budget.
- **Observability**: dim log lines scroll past during heavy tool output. The
  `/context` eviction/compaction counters made firings verifiable — kept as a
  permanent feature, not just a test aid.

### v0.3 candidates (from dogfood)
- Cache conversation history too (CC does; minicc caches only the stable prefix
  to avoid eviction thrashing the cache).
- Reduce/evict large *tool_use* inputs (e.g. write_file content) — L3 only
  touches tool_result, not tool_use, so a big write stays full until it ages
  into the compacted portion.
- Softer thrash recovery (auto-`/clear`, or drop the oversized block + retry)
  instead of crashing the turn.

## Implementation order (Order B)

Justified in detail elsewhere; tl;dr is "smallest commit + most urgent dogfood
relief first":

1. **L2** (commit) — tools/ only; immediate rate-limit relief from single-call
   bloat
2. **L3** (commit) — llm.py only; accumulated history defense
3. **L6a `/context`** (commit) — cli.py only; user visibility
4. **L1** (commit) — 5 files; cost optimization + CLAUDE.md project memory
5. **L4** (commit) — llm.py only; LLM-based summarization as L3 fallback
6. **L5** (commit) — llm.py only; thrashing safety net on L4
7. **L6b `/compact` + L6c `/recap`** (commit) — cli.py only; expose L4 to user

After all 7: tag `v0.2`.

## Updating this doc

When implementing a layer:
1. Flip ⬜ Planned → ✅ Implemented in the table
2. Fill in any TBD numbers under "minicc's own choices"
3. Reference this doc by section in the commit message:
   ```
   M7 L3: tool_result eviction
   
   See CONTEXT_MANAGEMENT.md#the-6-layers for L1-L6 overview.
   ```