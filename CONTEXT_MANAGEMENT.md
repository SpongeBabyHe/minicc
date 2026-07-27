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
[context-editing]: https://platform.claude.com/docs/en/build-with-claude/context-editing

Implementation is split by responsibility under `minicc/context_management/`:
`budget.py` owns sizing and `ContextState`, `eviction.py` owns local tool-result
edits, `summary.py` owns safe cuts and cache-compatible summary requests, and
`manager.py` owns hooks, transcript persistence, and high-level compaction.
The package `__init__.py` is the single public entry point for callers.

## The layers at a glance

| Layer  | Concern | What it does                                                                                                                                                                                        |
| ------ | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **L1** | cost    | Prompt cache: stable prefix (system+tools, session env/git) **and** conversation history. CLAUDE.md/memory/skills ride `<system-reminder>` messages, not prefix layers (reminders.py).              |
| **L2** | size    | Cap each tool's output at the source so one call can't flood the history.                                                                                                                           |
| **L3** | size    | **Auto, incremental:** above 100K, clear eligible old `tool_result` content only when net savings reach 20K; preserve five recent results and all stateful-tool results.                      |
| **L4** | size    | **Primary lever:** when context nears the model window, summarize a safe old prefix into a fresh, shorter one while preserving matching cache layers.                                               |
| **L5** | safety  | Stop after N failed compactions; recover structurally oversized requests on 400/413 without treating 429 as a size signal.                                                                          |
| **L6** | control | `/context`, `/compact [focus]`, `/recap` — visibility and manual control.                                                                                                                           |

L1 runs passively every turn; L2 on every tool call. The size path is **two-band,
like CC** — which "clears older tool outputs first, then summarizes the
conversation if needed": above
`TOOL_RESULT_EVICTION_TRIGGER_TOKENS` **L3** can evict eligible old tool outputs,
and only when the *real* context size still nears the model window does **L4
compaction** reset the history. L5 guards thrash + reactive 400/413 recovery. L6
is the user's hand on the wheel.

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
- **Session context** — a second breakpoint after the env/git block
  (`build_session_context`: cwd, platform, and a git snapshot — CC keeps env +
  gitStatus in its system prompt too). It's **volatile-last** — placed after the
  static layer so its only change (a `/clear` refresh) can't bust system+tools
  above it. The env used to be baked into the static system prompt, which mixed
  per-session content into the most-stable layer.
- **Conversation history** — `_cacheable` marks the last block of the most-recent
  message every turn (the standard rolling-breakpoint pattern). The next turn
  reads the whole prior history from cache; only the new exchange is fresh.

**CLAUDE.md, the auto-memory MEMORY.md index, the current date, and the skill
listing are NOT prefix layers** (they were, until 2026-07-16). CC delivers them
as `<system-reminder>` blocks in the *message stream* — its memory doc says it
outright: *"CLAUDE.md content is delivered as a user message after the system
prompt, not as part of the system prompt itself"* — and minicc now does the
same (`reminders.py`). Re-injection is per-kind, matching each documented CC
behavior: the **skills listing** re-injects when the skill set changes
(`watch.py` mtime poll — skills have documented live change detection); the
**claudeMd block** re-injects only when its copy is LOST from the live history
(post-compact — *"Claude re-reads it from disk and re-injects it"* — /clear,
resume, rewind), never on a mid-session edit. Either way the update is one
appended block instead of a busted prefix layer.

That uses **three** of the API's four breakpoints per request (system+tools /
session / conversation) — the static→dynamic layer stack CC uses, one spare. A
would-be *conversation anchor* (a second history breakpoint to stay inside the
20-block cache lookback on ≈10+ parallel-tool turns) now HAS a free slot — the
reminder refactor released the old project-context breakpoint — but stays
deferred on the merits: minicc re-marks the last message every call, so
consecutive requests differ by only a couple of blocks and the common case is
already inside the lookback window. It's a dogfood-signal-first change now,
not a budget problem.

**Economics.** Cache write costs 1.25× input, read 0.1×, break-even at two
requests. A 50K-token history on Sonnet ($3/M in) is ~$0.15/turn uncached vs
~$0.015 cached — ~90% off the history, the write paid once. It compounds: every
later turn (and all dogfood) gets cheaper.

**TTL.** The default cache lives 5 minutes (each hit refreshes it free); a think
break longer than that re-pays the write. Setting `cache_ttl: "1h"` in settings
(GA, no beta header) puts the **stable prefix layers** on the 1-hour tier — written
once at 2× instead of 1.25×, then refreshed free — while the **rolling conversation
breakpoint stays at 5m**, because the API requires longer-TTL breakpoints to precede
shorter ones and the stable layers render first. Default is 5m (cost-neutral);
flip it for long interactive sessions with gaps. (CC requests 1h automatically on
subscriptions.)

**L1×L3 tension, managed by minimum net savings.** In-place eviction rewrites the
cached prefix mid-history and breaks the cache from that point — so evicting on
*every* turn would re-pay the write for a trickle of freed tokens. L3 only edits
when aggregate **net** reclaim reaches
`TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS = 20K`; this is the local equivalent of
the public API's optional [`clear_at_least`][context-editing] guard. Most turns
therefore leave the prefix untouched. Compaction (L4) is the larger reset: it
replaces history with a summary and establishes a fresh shorter prefix. The
over-budget branch deliberately **skips** normal eviction that turn so the
summary fork can reuse the unedited prefix.

## Size: caps, eviction, compaction (L2–L5)

**L2 — cap output at the source.** Every tool bounds what it returns so a single
call can't flood the history: bash 30K chars (full output saved to
`.minicc/bash_outputs/` + a 2K preview), glob 100 matches, grep 100/file + 50K,
read_file 50K with a truncation notice and `offset`/`limit` for windowed reads.
This is the first and cheapest defense — it keeps junk *out* of the history
rather than removing it later.

**L3 — evict stale tool results (auto, incremental, savings-guarded).**
`plan_tool_result_eviction` first computes a mutation-free snapshot plan; only if
its aggregate net saving reaches 20K does `apply_tool_result_eviction` replace old
contents. Planning and context sizing share one UTF-8 byte estimator, avoiding
different decisions for non-ASCII output. Applying a stale plan aborts as a whole
rather than partially breaking the cache below the guard. The local trigger is
100K and the most recent five **eligible** results stay intact. Eligibility follows
CC 2.1.212's re-fetchable-tool policy mapped to minicc: `read_file`, `bash`,
`grep`, `glob`, `web_fetch`, `edit_file`, and `write_file`. Stateful `agent`,
memory, skill, and task-coordination results are never cleared. `web_search` is
server-side in minicc (`server_tool_use` + `web_search_tool_result` inside the
assistant message), so this client-result rewriter deliberately leaves its
encrypted replay blocks unchanged.

The tool_use → tool_result structure and tool inputs remain visible. In the main
session, every original result is saved under `.minicc/tool_outputs/` and the
`context_edit` replay delta is logged **before** the working set changes. A normal
L3 event aborts unchanged if either step fails; only rejected-request recovery
explicitly permits CC's lossy `[Old tool result content cleared]` fallback.
No-session/subagent eviction uses that marker directly. Exact marker-shape
matching prevents ordinary tool output that merely starts with
`<persisted-output>` from being mistaken for an earlier edit. The original `msg`
event remains lossless. Each candidate must save space individually; a large
result cannot hide tiny rewrites that would grow context. Once size crosses the
compaction budget, L4 takes over and normal L3 stands down for that turn.

**L4 — compaction (the primary lever).** The trigger anchors the last response's
real `usage` (`input + cache_read + cache_creation`) to the estimated size of the
exact messages sent, then adds the estimated message delta. A newly appended large
user/tool message is therefore visible immediately instead of one turn late. When
that prediction nears the effective budget
`model_window − max_output_tokens − 13K`, compaction summarizes older history into
CC's **9-section** note (Primary Request & Intent / Key Technical Concepts / Files & Code /
Errors & fixes / Problem Solving / All user messages / Pending Tasks / Current Work /
Optional Next Step) and replaces it. `KEEP_RECENT_MESSAGES_TARGET = 6` is a target,
not an invariant. The planner starts at the nearest safe assistant boundary, moves
forward only as far as needed to reclaim the predicted over-budget amount, and
moves backward if the complete summary request would not fit. It can therefore
keep fewer messages when the oversized block sits in the nominal recent tail, or
more when one summary call cannot cover the preferred prefix. The budget is
**window-relative like CC, with no sub-window clamp**
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` can lower the capacity used for this calculation;
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1–100) can trigger earlier, but never later than
the default threshold. Both names match Claude Code for drop-in environment config.
Concretely, the messages before the cut collapse into a single
`[Earlier conversation summary]` user message, prepended to the kept tail:

```
before:  system+tools │ older messages (msg_0 … cut) │ recent
after:   system+tools │ summary                       │ recent
```

This touches the cache at **two moments that are easy to conflate**:

1. *Generating the summary reuses what can safely match.* `_summarize` sends
   **only `messages[:cut]`**, the part being replaced, under the same model,
   system, tools, and thinking mode, with the instruction appended. Cut fitting
   measures that **complete request** — system, tools, cached messages,
   instruction, thinking configuration, and retry output reserve — rather than
   assuming a fixed prefix-only margin. This avoids summary/raw-tail overlap and
   the fatal reactive pattern of resending the rejected full body. Matching
   system/tool/message prefixes can still hit cache, but a full-history cache hit
   is not promised. The prompt requires text-only output; an empty, tool-call, or
   truncated response is rejected and retried once with `tool_choice:none` and a
   larger output limit.
2. *Living with the result is a fresh, shorter cache.* The next real turn sends
   the new `[summary] + recent` history. That prefix no longer matches the old
   one, so the conversation cache is rebuilt once — only system + tools carry over
   — and every later turn reuses the new short prefix. This is the **intentional
   reset**: compaction discards the old history *by design* (that is the
   shrinking), then caches cleanly again. (Durable facts meant to outlive this
   reset are the job of **auto-memory (MEMORY.md)** — its index re-loads from disk
   on `/clear`, CC's "project memory survives compaction".)

A structural rule keeps the replacement valid: the cut lands on an **assistant
boundary**, so the kept tail starts with an assistant message and prepending the
summary (a user message) preserves role alternation without splitting any
tool_use/tool_result pair. (Cutting at a user tool_result would orphan it — the
original cause of a thrash bug on long single turns.)

**Reactive recovery (400 / 413 fallback).** If a send still fails with **400
`"prompt is too long"`** (token overflow) or **413 `request_too_large`** (32 MiB
byte limit), minicc first evicts positive-savings tool results locally, then
summarizes bounded safe prefixes until the rewritten live request is below the
corresponding safety line. Only then does it retry the live request once. A
summary request therefore never repeats the already rejected full body. Recovery
is capped at `MAX_RECOVERY_COMPACTIONS = 8`; an irreducible message or exhausted
cap surfaces the original API error. Generic 400s and **all 429s** propagate
unchanged after SDK retries.

Compaction also fires **PreCompact / PostCompact hooks** (HOOKS.md): a PreCompact
hook can veto a compaction (exit 2 / `decision:"block"`); a hook that vetoes
*persistently* does **not** increment L5's breaker because a veto is a user
decision, not a failed compaction. `continue:false` instead aborts processing by
the hook's universal contract.

**L5 — thrash guard.** If the history is still over budget after
`MAX_COMPACT_ATTEMPTS = 3` compactions in a row, minicc raises (with a pointer to
`/clear` or smaller chunks) instead of looping forever.

*Is L5 even reachable?* Within the effective budget, essentially not — the
upstream layers bound every block that compaction would need to shrink:

- **Tool outputs** are capped at the source (read_file/grep 50K chars ≈ 12K
  tokens, bash 30K + disk) — far under budget.
- **Tool-use inputs** (e.g. a big `write_file` content) escape L2, but the model
  emits them under `max_tokens = 16000`, so a single one is bounded — also far
  under budget.
- The usual recent-tail target is bounded by those same per-message limits.

So compaction can always pull accumulated history below the budget. The one path
that stays open is a **single, irreducible, oversized message**, and the only
unbounded source of one is **user input**: L2 caps tool *output* but not what the
user types, and L4 can't cut the most-recent turn (`_find_cut_index` cuts *before*
it). A pasted oversized message can't be shrunk, so reactive 400/413 recovery
forces a compaction that can't reduce and surfaces the original error.

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
older tool outputs first, then a
**`window − max output − 13K` compaction trigger** only if still needed; real
usage anchored to estimated message growth; compatible window/percentage
environment overrides; the **9-section** compact summary;
cache layers system / session /
conversation (env/git volatile-last; CLAUDE.md rides `<system-reminder>`
messages, loaded in full — the 200-line/25 KB cap is MEMORY.md-only);
`/compact [focus]`; `/recap` is cache-safe.

**Version-aligned local policy:** public API context editing defaults to a 100K
trigger and keep-3, but the observable CC 2.1.212 local fallback uses a 20K
minimum saving, keep-5, the exact old-result marker above, and a re-fetchable-tool
allowlist. minicc uses 100K as its explicit local trigger and adopts the remaining
2.1.212 fallback shape. The private 75K `target_tokens_saved` value belongs to a
server `context_hint`; it is not a third local hard gate.

**minicc's own** (CC silent, or the value is minicc's judgment):
`KEEP_RECENT_MESSAGES_TARGET = 6` (a safe structural target, not an invariant);
the exact summary wording; `MAX_COMPACT_ATTEMPTS = 3`; spilling evicted main-session
results under `.minicc/tool_outputs/`; and the `.minicc/` self-ignoring directory
(`.minicc/.gitignore: "*"`, so artifacts never get tracked even if the project
forgets to ignore them).

## Not yet implemented

The backlog — each feature with what it buys and why it waits.

| Feature                                                                                             | What it buys                                                               | Why deferred                                                                                                                                                             |
| --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Auto-triggered consolidation** (CC: background, >24h AND >5 sessions — community-observed)        | memory tidies itself without a manual command                              | `/memory consolidate` (manual) shipped; wire an idle trigger once dogfood shows the cadence                                                                              |
| **Server context-hint A/B**                                                                          | compare local fallback with CC's live private controller                   | remote feature gates and server policy cannot be inferred from the binary; requires captured real requests                                                              |
| **Dynamic cache breakpoint** (conversation anchor)                                                  | a 2nd history breakpoint for the 20-block lookback on block-heavy turns    | a slot is FREE since the reminder refactor released the project-context breakpoint (2026-07-16); still marginal (minicc re-marks every call) — wait for a dogfood signal |
| **User-input source cap**                                                                           | bound the one unbounded input (a huge pasted message) so it can't reach L5 | L5 already backstops it; turns a hard failure into a graceful one                                                                                                        |
| Server-side compaction (`compact-2026-01-12`)                                                       | summarize server-side, no extra round-trip                                 | build-vs-buy — the hand-rolled L4 is the portfolio substance; server-side is the production swap                                                                         |

(Now implemented and folded into the design above: conversation-history caching,
**two-band sizing** — allowlisted, savings-guarded L3 eviction, then a
prefix-preserving **L4 compaction** on a
**`window − max output − 13K` trigger + anchored token accounting**, the
**9-section** summary, bounded **reactive 400/413** recovery, and the
compaction-correctness fixes. Subagents,
`/rewind` + file checkpoints, session persistence, the session-context cache layer,
and **auto-memory** (the `memory` tool + MEMORY.md index, gated writes) are also done
— see [SUBAGENTS.md](SUBAGENTS.md), [CHECKPOINT.md](CHECKPOINT.md), `sessions.py`,
and `memory.py`.)

## Dogfood lessons

From dogfood on llm-kaki (raw in-the-moment jots in `PAIN.md`):

- **Caching works** — `cache_read` went 0 → 1465 across two turns (~41%
  cumulative hit); the stable-prefix breakpoints behave as intended.
- **CLAUDE.md shapes behavior** — asked to write path code, the model reached for
  `pathlib` and noted "no os.path needed" *unprompted*; it had absorbed
  CLAUDE.md's rule. Redundant re-reads happen only on meta-questions, not real work.
- **Eviction is graceful** — the model treats the evicted marker as "re-fetch if
  needed" and re-reads instead of confabulating. Tradeoff: occasional extra
  reads; revisit `TOOL_RESULT_EVICTION_KEEP_RECENT` if they get frequent.
- **The thrash failsafe fires** — at a 3K test budget, a 16K-char single file
  drove 6 compaction attempts then a clean L5 raise. Verified via the `/context`
  counters, not by hunting dim log lines.
- **Eviction/compaction suit SEQUENTIAL tasks, not SURVEY tasks** — "read &
  report each, move on" coexists with eviction; "hold all files at once to
  answer" fights it (re-read churn → thrash). Don't set the budget below the
  task's working set.
