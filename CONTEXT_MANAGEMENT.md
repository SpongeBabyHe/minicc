# Context management

An LLM agent re-sends its **entire** conversation on every API call, and that
history only grows — each tool call and its output joins it. Two distinct
pressures follow, and minicc keeps them apart because the fixes are different:

- **Size** — the history must fit the model's context window and per-minute rate
  limits.
- **Cost** — input tokens are billed every turn, so a long history is paid for
  again and again.

The load-bearing distinction: **caching cuts cost without shrinking anything;
eviction and compaction cut size.** You need both. The design mirrors Claude
Code's documented behavior — where CC publishes specifics minicc follows them,
where it's silent minicc makes (and labels) its own choice.

[cc-cache]: https://code.claude.com/docs/en/prompt-caching
[cache-tool]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching

## The layers at a glance

| Layer | Concern | What it does |
| ----- | ------- | ------------ |
| **L1** | cost | Prompt cache: stable prefix (system+tools, project) **and** conversation history. CLAUDE.md as project memory. |
| **L2** | size | Cap each tool's output at the source so one call can't flood the history. |
| **L3** | size | Above a token budget, blank out old `tool_result` content (keep the structure). No LLM call. |
| **L4** | size | If eviction isn't enough, summarize the history with one LLM call. |
| **L5** | safety | Stop compacting after N attempts if a single message keeps refilling the window. |
| **L6** | control | `/context`, `/compact [focus]`, `/recap` — visibility and manual control. |

L1 runs passively on every turn; L2 on every tool call; **L3 → L4 → L5** trigger
in sequence as the history grows past `TOKEN_BUDGET`; L6 is the user's hand on
the wheel.

## Cost: prompt caching (L1)

The API caches by **exact prefix match**: mark a block with `cache_control` and
the server stores the processed state of everything up to that block, keyed by a
hash; a later request whose prefix is byte-identical reads it back at ~0.1× input
price instead of recomputing. The request renders in a fixed hierarchy —
**`tools → system → messages`** — and a change at any level invalidates that
level and everything after it (the [invalidation table][cache-tool] proves the
order: changing tools busts system+messages too, but changing system leaves the
tools cache intact).

minicc places breakpoints to match how often each region changes:

- **Stable prefix** — one breakpoint on the last `system` block. Because tools
  render *before* system, that breakpoint's prefix already covers the tool
  definitions, so they cache together as one "system + tools" layer (the way CC
  groups them). A separate tools breakpoint would only help if system changed
  while tools didn't — which never happens within a session, since the system
  prompt is frozen at construction.
- **Project context** — a second breakpoint after CLAUDE.md (first 200 lines /
  25 KB). It changes only on `/clear`, so keeping it separate lets a CLAUDE.md
  reload re-cache while the system+tools layer survives.
- **Conversation history** — `_cacheable` marks the last block of the most-recent
  message every turn (the standard rolling-breakpoint pattern). The next turn
  reads the whole prior history from cache; only the new exchange is fresh.

That is three of the API's **four** breakpoints per request, leaving one free.
The spare is reserved for a future *conversation anchor* — a second history
breakpoint to stay within the API's 20-block cache lookback when one turn appends
many blocks (≈10+ parallel tool calls). It's deferred, not built: minicc re-marks
the last message on every call, so consecutive requests differ by only a couple
of blocks and the common case is already inside the lookback window.

**Economics.** Cache write costs 1.25× input, read 0.1×, break-even at two
requests. A 50K-token history on Sonnet ($3/M in) is ~$0.15/turn uncached vs
~$0.015 cached — ~90% off the history, the write paid once. It compounds: every
later turn (and all dogfood) gets cheaper.

**The tension with eviction.** L3 rewrites old `tool_result` content *in place*,
changing the cached prefix bytes and invalidating the cache from that point —
caching and incremental eviction pull against each other. Resolved by keeping
eviction rare and coarse:

- L3 fires only above `TOKEN_BUDGET` (150K); below that — the common session —
  caching is clean and L3 never runs.
- L4 compaction is an intentional reset: it replaces the history with a summary
  (one re-write), then the shorter prefix caches cleanly again.
- Only L3's incremental eviction needs real coordination, and only near the
  rate-limit wall. CC's server-side context editing names the knob —
  `clear_at_least`: break the cache only when eviction frees enough to be worth
  the re-write. A cheap thing minicc could adopt; today it accepts one re-write
  per eviction event.

## Size: caps, eviction, compaction (L2–L5)

**L2 — cap output at the source.** Every tool bounds what it returns so a single
call can't flood the history: bash 30K chars (full output saved to
`.minicc/bash_outputs/` + a 2K preview), glob 100 matches, grep 100/file + 50K,
read_file 50K with a truncation notice and `offset`/`limit` for windowed reads.
This is the first and cheapest defense — it keeps junk *out* of the history
rather than removing it later.

**L3 — evict stale tool results.** When the estimated history
(`len(json.dumps(messages)) // 4`) exceeds `TOKEN_BUDGET` (150K), the oldest
`tool_result` contents are replaced with a marker, keeping the
`RECENT_TOOL_RESULTS_KEEP = 4` most recent intact. The assistant tool_use → user
tool_result structure stays, so the model still sees that a tool ran and can
re-call it; only the bytes are gone. No LLM call.

**L4 — compaction.** If eviction isn't enough, compaction summarizes the older
history into a fixed-shape note (Goal / Done / Key findings / In progress / Open
questions) and replaces it, keeping `KEEP_RECENT_MESSAGES = 6` recent messages
verbatim. Concretely, the messages before the cut collapse into a single
`[Earlier conversation summary]` user message, prepended to the kept tail:

```
before:  system+tools │ older messages (msg_0 … cut) │ recent
after:   system+tools │ summary                       │ recent
```

This touches the cache at **two moments that are easy to conflate**:

1. *Generating the summary is cheap.* The summarization call runs on the
   **full, pre-replacement** history — `_summarize` re-sends the same system +
   tools + history with the instruction appended as a final user message (the way
   CC does it), so its prefix matches the live conversation and **reads the
   existing history cache**; only the appended instruction is new. (The old
   approach — flattening history to capped text in a fresh request — re-paid for
   the whole history.) The hit holds unless L3 rewrote the prefix earlier this
   same turn, in which case it degrades to a normal same-sized request.
2. *Living with the result is a fresh, shorter cache.* The next real turn sends
   the new `[summary] + recent` history. That prefix no longer matches the old
   one, so the conversation cache is rebuilt once — only system + tools carry over
   — and every later turn reuses the new short prefix. This is the **intentional
   reset** that defuses the L1×L3 tension: compaction discards the old history *by
   design* (that is the shrinking), then caches cleanly again.

A structural rule keeps the replacement valid: the cut lands on an **assistant
boundary**, so the kept tail starts with an assistant message and prepending the
summary (a user message) preserves role alternation without splitting any
tool_use/tool_result pair. (Cutting at a user tool_result would orphan it — the
original cause of a thrash bug on long single turns.)

**L5 — thrash guard.** If the history is still over budget after
`MAX_COMPACT_ATTEMPTS = 3` compactions in a row, minicc raises (with a pointer to
`/clear` or smaller chunks) instead of looping forever.

*Is L5 even reachable?* In the normal 150K range, essentially not — the upstream
layers bound every block that L3 + L4 would need to shrink:

- **Tool outputs** are capped at the source (read_file/grep 50K chars ≈ 12K
  tokens, bash 30K + disk) and evicted by L3 once old — far under budget.
- **Tool-use inputs** (e.g. a big `write_file` content) escape L2 and L3, but the
  model emits them under `max_tokens = 8000`, so a single one is ≤ ~32K chars —
  also far under budget.
- The kept-recent window (`KEEP_RECENT_MESSAGES = 6`) is bounded by those same
  per-message limits.

So L3 + L4 can always pull accumulated history below 150K. The one path that
stays open is a **single, irreducible, oversized message**, and the only
unbounded source of one is **user input**: L2 caps tool *output* but not what the
user types, L3 only touches `tool_result`, and L4 can't cut the most-recent turn
(`_find_cut_index` cuts *before* it). A pasted 200K-char message can't be shrunk
by any layer, so after three futile compactions L5 raises.

L5 is therefore a real failsafe, not dead code — it backstops exactly the input
the capacity layers don't bound. Closing that gap (a source cap / chunking on
user messages, or evicting oversized `tool_use` inputs) would push L5 toward
never firing.

## User controls (L6)

- **`/context`** — current usage plus durable eviction/compaction event counters,
  so you can see L3/L4 fire without hunting dim log lines.
- **`/compact [focus]`** — manual compaction; the optional focus steers what the
  summary preserves.
- **`/recap`** — summarizes the conversation *without* mutating it, so the cached
  prefix stays intact.

## Verified from CC vs minicc's own choices

**Following CC** (where it publishes specifics): bash output cap + disk save;
glob 100 by mtime; read `offset`/`limit` pagination; clear tool outputs before
summarizing (L3 → L4); cache layers system / project / conversation; CLAUDE.md
first 200 lines or 25 KB; `/compact [focus]`; `/recap` is cache-safe.

**minicc's own** (CC silent): `TOKEN_BUDGET = 150_000` as a single threshold,
estimated via `len(json) // 4`; `RECENT_TOOL_RESULTS_KEEP = 4`;
`KEEP_RECENT_MESSAGES = 6`; the summary template; `MAX_COMPACT_ATTEMPTS = 3`; the
`.minicc/` self-ignoring directory (`.minicc/.gitignore: "*"`, so artifacts never
get tracked even if the project forgets to ignore them).

## Not yet implemented

| Feature | Why CC has it | Why minicc defers |
| ------- | ------------- | ----------------- |
| MEMORY.md (auto-written memory) | learnings persist across sessions | needs automatic writeback; design wants dogfood data |
| Server-side compaction (`compact-2026-01-12` beta) | summarize server-side, no extra round-trip | L4's self-rolled (now prefix-shared) summary suffices for now |
| L3×caching coordination (`clear_at_least`) | break the cache only when eviction pays off | accept one re-write per eviction event; only bites near the budget wall |

(Subagents, `/rewind` + file checkpoints, and session persistence — once on this
list — are now implemented; see [SUBAGENTS.md](SUBAGENTS.md),
[CHECKPOINT.md](CHECKPOINT.md), and `sessions.py`.)

## Dogfood lessons

From dogfood on llm-kaki (raw in-the-moment jots in `PAIN.md`):

- **Caching works** — `cache_read` went 0 → 1465 across two turns (~41%
  cumulative hit); the stable-prefix breakpoints behave as intended.
- **CLAUDE.md shapes behavior** — asked to write path code, the model reached for
  `pathlib` and noted "no os.path needed" *unprompted*; it had absorbed
  CLAUDE.md's rule. Redundant re-reads happen only on meta-questions, not real work.
- **Eviction is graceful** — the model treats the evicted marker as "re-fetch if
  needed" and re-reads instead of confabulating. Tradeoff: occasional extra
  reads; raise `RECENT_TOOL_RESULTS_KEEP` if they get frequent.
- **The thrash failsafe fires** — at a 3K test budget, a 16K-char single file
  drove 6 compaction attempts then a clean L5 raise. Verified via the `/context`
  counters, not by hunting dim log lines.
- **Eviction/compaction suit SEQUENTIAL tasks, not SURVEY tasks** — "read &
  report each, move on" coexists with eviction; "hold all files at once to
  answer" fights it (re-read churn → thrash). Don't set the budget below the
  task's working set.
