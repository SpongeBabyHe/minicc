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
| **L3** | size | **Auto, incremental:** above `CLEAR_TRIGGER` blank out the oldest `tool_result` content — `clear_at_least`-guarded so it only breaks the cache when it frees enough (CC: clear tool outputs first). |
| **L4** | size | **Primary lever:** when context nears the model window, summarize the history into a fresh, shorter prefix (on a warm cache). |
| **L5** | safety | Stop after N compactions if one message keeps refilling the window; plus a reactive compaction on a 413. |
| **L6** | control | `/context`, `/compact [focus]`, `/recap` — visibility and manual control. |

L1 runs passively every turn; L2 on every tool call. The size path is **two-band,
like CC** — which "clears older tool outputs first, then summarizes the
conversation if needed": above `CLEAR_TRIGGER` **L3** incrementally evicts old tool
outputs every turn, and only when the *real* context size still nears the model
window does **L4 compaction** reset the history. L5 guards thrash + a 413. L6 is
the user's hand on the wheel.

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
  reload re-cache while the system+tools layer survives. The planned **MEMORY.md
  index** (cross-session memory — see "Not yet implemented") is designed to load
  here too, beside CLAUDE.md, riding the same project-context cache.
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

**L1×L3 tension, managed by `clear_at_least`.** In-place eviction rewrites the
cached prefix mid-history and breaks the cache from that point — so evicting on
*every* turn would re-pay the write for a trickle of freed tokens. minicc handles
this the way CC's server-side `clear_tool_uses` does, with a `clear_at_least`
guard: L3 only evicts when the reclaim clears the bar (≥ `CLEAR_AT_LEAST` tokens),
so most turns don't touch the prefix at all and the cache survives. Compaction (L4)
is the larger *reset* — it replaces the history with a summary, breaking the cache
**once**, after which the new shorter prefix caches cleanly. And the over-budget
branch that runs L4 deliberately **skips** eviction that turn, so the summarization
call still reads a warm prefix (see L4 below).

## Size: caps, eviction, compaction (L2–L5)

**L2 — cap output at the source.** Every tool bounds what it returns so a single
call can't flood the history: bash 30K chars (full output saved to
`.minicc/bash_outputs/` + a 2K preview), glob 100 matches, grep 100/file + 50K,
read_file 50K with a truncation notice and `offset`/`limit` for windowed reads.
This is the first and cheapest defense — it keeps junk *out* of the history
rather than removing it later.

**L3 — evict stale tool results (auto, incremental, `clear_at_least`-guarded).**
`_evict_old_tool_result` blanks the oldest `tool_result` contents (keeping
`RECENT_TOOL_RESULTS_KEEP = 4`), preserving the tool_use → tool_result structure
so the model can re-call. It runs **every turn the real context size sits between
`CLEAR_TRIGGER = 100K` and the compaction budget** — CC's "clear older tool outputs
first." It's cheap (no LLM call), and the `min_free = CLEAR_AT_LEAST` guard means it
only rewrites the prefix when the eviction frees ≥ that many tokens, so it never
breaks the cache for a small gain (see the L1×L3 note above). When the size crosses
the budget instead, L4 takes over and L3 stands down for that turn.

**L4 — compaction (the primary lever).** When the **real** context size — the
last response's `usage` (`input + cache_read + cache_creation`), not a
char-estimate — nears the **effective budget** `min(95% × model_window,
SAFE_REQUEST_CEILING ≈ 350K)`, compaction summarizes the older history into a
fixed-shape note (Goal / Done / Key findings / In progress / Open questions) and
replaces it, keeping `KEEP_RECENT_MESSAGES` recent messages verbatim. The budget
is **window-relative** (like CC) but **clamped** by the ceiling, because minicc's
endpoint has a real ~450K single-request wall (PAIN.md) — the clamp generalizes
minicc's old absolute 150K (per-model: Haiku→190K, Sonnet/Opus 1M→350K).
Concretely, the messages before the cut collapse into a single
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
   the whole history.) The over-budget branch that triggers compaction
   deliberately skips L3 eviction that turn, so no in-place rewrite precedes it and
   the prefix is reliably warm here.
2. *Living with the result is a fresh, shorter cache.* The next real turn sends
   the new `[summary] + recent` history. That prefix no longer matches the old
   one, so the conversation cache is rebuilt once — only system + tools carry over
   — and every later turn reuses the new short prefix. This is the **intentional
   reset**: compaction discards the old history *by design* (that is the
   shrinking), then caches cleanly again. (Durable facts meant to outlive this
   reset are the job of the planned **MEMORY.md** — re-read from disk after
   compaction, CC's "project memory survives compaction".)

A structural rule keeps the replacement valid: the cut lands on an **assistant
boundary**, so the kept tail starts with an assistant message and prepending the
summary (a user message) preserves role alternation without splitting any
tool_use/tool_result pair. (Cutting at a user tool_result would orphan it — the
original cause of a thrash bug on long single turns.)

**Reactive compaction (413 fallback).** If a request still returns **413
(request-too-large)** — the real-token trigger under-fired, or a single turn
exceeded the ceiling — `llm_response` catches it, forces one compaction, and
retries once (the SDK does **not** auto-retry 413). A second 413 (one
un-compactable huge message) surfaces. This is the safety net for accounting
drift on top of the proactive window-relative trigger.

**L5 — thrash guard.** If the history is still over budget after
`MAX_COMPACT_ATTEMPTS = 3` compactions in a row, minicc raises (with a pointer to
`/clear` or smaller chunks) instead of looping forever.

*Is L5 even reachable?* Within the effective budget, essentially not — the
upstream layers bound every block that compaction would need to shrink:

- **Tool outputs** are capped at the source (read_file/grep 50K chars ≈ 12K
  tokens, bash 30K + disk) — far under budget.
- **Tool-use inputs** (e.g. a big `write_file` content) escape L2, but the model
  emits them under `max_tokens = 8000`, so a single one is ≤ ~32K chars — also far
  under budget.
- The kept-recent window (`KEEP_RECENT_MESSAGES`) is bounded by those same
  per-message limits.

So compaction can always pull accumulated history below the budget. The one path
that stays open is a **single, irreducible, oversized message**, and the only
unbounded source of one is **user input**: L2 caps tool *output* but not what the
user types, and L4 can't cut the most-recent turn (`_find_cut_index` cuts *before*
it). A pasted 200K-char message can't be shrunk, so reactive-413 forces a
compaction that can't reduce, and after `MAX_COMPACT_ATTEMPTS` L5 raises.

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
glob 100 by mtime; read `offset`/`limit` pagination; **two-band sizing** — clear
older tool outputs first (`clear_at_least`-guarded), then a **window-relative
compaction trigger** (~95% of the model window) only if still needed; real token
accounting from `usage`; cache layers system / project / conversation; CLAUDE.md
first 200 lines or 25 KB; `/compact [focus]`; `/recap` is cache-safe.

**minicc's own** (CC silent on the exact values): the **`SAFE_REQUEST_CEILING ≈
350K`** clamping the window-relative budget (minicc's endpoint wall, PAIN.md); the
L3 thresholds `CLEAR_TRIGGER = 100K` / `CLEAR_AT_LEAST = 5K` (the *ordering* is CC's,
these numbers are minicc's); `RECENT_TOOL_RESULTS_KEEP = 4`; `KEEP_RECENT_MESSAGES`;
the summary template; `MAX_COMPACT_ATTEMPTS = 3`; the `.minicc/` self-ignoring
directory (`.minicc/.gitignore: "*"`, so artifacts never get tracked even if the
project forgets to ignore them).

## Not yet implemented

The backlog — each feature with what it buys and why it waits. The substantial
one is **MEMORY.md** (auto-written cross-session memory).

| Feature | What it buys | Why deferred |
| ------- | ------------ | ------------ |
| **MEMORY.md** (auto-written cross-session memory) | learnings persist across sessions (context-engineering principle #6) | a real feature (~1 week); "what to persist" wants dogfood data |
| **Tuning** (`KEEP_RECENT_MESSAGES` 6→10, summary 5→9 sections, `CLEAR_TRIGGER`/`CLEAR_AT_LEAST`) | closer to CC's defaults | low-risk polish; wants dogfood data; **future work** |
| **Dynamic cache breakpoint** (conversation anchor) | spend the freed 4th breakpoint to stay inside the 20-block lookback on block-heavy turns | marginal (minicc re-marks every call); the cache-hit gain can't be unit-verified — wait for a dogfood signal |
| **User-input source cap** | bound the one unbounded input (a huge pasted message) so it can't reach L5 | L5 already backstops it; turns a hard failure into a graceful one |
| Server-side compaction (`compact-2026-01-12`) | summarize server-side, no extra round-trip | build-vs-buy — the hand-rolled L4 is the portfolio substance; server-side is the production swap |

(Now implemented and folded into the design above: conversation-history caching,
**two-band sizing** — incremental L3 eviction with `clear_at_least`, then a
prefix-sharing **L4 compaction** on a **window-relative trigger + real token
accounting** — **reactive-413**, and the compaction-correctness fixes. Subagents,
`/rewind` + file checkpoints, and session persistence are also done — see
[SUBAGENTS.md](SUBAGENTS.md), [CHECKPOINT.md](CHECKPOINT.md), and `sessions.py`.)

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
