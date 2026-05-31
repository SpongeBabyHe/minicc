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

| Layer | Name                             | Status        | What it does                                                                                                                                      |
| ----- | -------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| L1    | 3-layer prompt cache + CLAUDE.md | ⬜ Planned     | Cache stable prefixes (system / project / tools) so cached input tokens cost ~10%. Load CLAUDE.md as project-level memory that survives `/clear`. |
| L2    | Tool output limits               | ✅ Implemented | Prevent a single tool call from blowing up context. Bash 30K + disk save; glob 100 cap; read_file truncation notice.                              |
| L3    | Tool_result eviction             | ⬜ Planned     | When history exceeds threshold, blank out content of old `tool_result` messages (keep structure so model knows it called the tool). No LLM call.  |
| L4    | LLM auto-compact                 | ⬜ Planned     | When eviction (L3) isn't enough, summarize the conversation via an LLM call. Replaces middle messages, preserves recent N turns.                  |
| L5    | Thrashing protection             | ⬜ Planned     | Stop auto-compact loop after N attempts if a single tool result keeps refilling the context.                                                      |
| L6    | User controls                    | ⬜ Planned     | `/context` (see usage), `/compact [focus]` (manual), `/recap` (summarize without mutating).                                                       |

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
- **Token thresholds for L3 eviction / L4 compact trigger**: TBD — will fix
  numbers when implementing L3.
- **Number of recent messages kept after L4 compact**: TBD.
- **L4 compact summary prompt**: minicc's own template — CC's prompt is
  proprietary.
- **L5 thrashing retry limit**: TBD (CC says "several").
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