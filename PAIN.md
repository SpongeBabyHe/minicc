# PAIN.md — raw dogfood scratchpad

10-second jots while using minicc. **Raw and unprocessed by design** — write
fast, don't analyze here. Processed understanding gets promoted out:
context-management lessons → `CONTEXT_MANAGEMENT.md`.

Format: `- YYYY-MM-DD: what happened`. Mark `FIXED` / `→ followup` inline.

---

## Prompt / model behavior
- 2026-05-28: CJK drift — Chinese query, model switched to Japanese mid-answer on
  technical content. FIXED (language anchor in system prompt, 9233b69).
- 2026-05-31: model claimed "no file > 2000 lines" after reading only 3–5 files;
  ignored glob's `[+N more chars]` truncation flag. → v0.2 prompt iter: add
  "verify exhaustively before claiming none exist". Workaround: ask for
  exhaustiveness explicitly.
- 2026-06-14: asked "what conventions does this project follow?" → model
  read_file'd CLAUDE.md even though it's injected. Redundant only on
  meta-questions; follows conventions silently when actually coding.

## Eval
- D2 test broken: model dodges edit_file multi-match by using write_file →
  multi-match recovery path stays untested. (evals/runs/20260526_145725_v1.log)
- F1 varies run-to-run: sometimes reads an existing tool for style first,
  sometimes not (sampling variance).

## Context management
- 2026-05-29: hit rate limit twice in < 1 hr on llm-kaki; "wait 1 min" didn't
  help (single request > 450K). → drove the whole v0.2 build.
  - 2026-07-01 re-diagnosed: it was an **ITPM rate limit (429)**, not a
    request-size wall — 450K fits Sonnet 4.6's 1M window (GA since 2026-03-13);
    the SDK re-sent the same oversized body, so waiting couldn't help. The 350K
    ceiling built on this was **dropped** for CC's `window − 13K` + reactive-429.
    FIXED (details in `CONTEXT_MANAGEMENT.md`).
- 2026-06-15: L4/L5 implemented & validated. Details (cut-point fix, survey-task
  churn, budget invariant, validation, v0.3 gaps) synthesized in
  `CONTEXT_MANAGEMENT.md` → "Dogfood lessons & validation".
- 2026-07-02: Phase-1 CC alignment landed (two-band L3/L4 with official
  context-editing defaults, `window − 13K` budget, 9-section summary, append-only
  session transcript, session-context cache layer, auto-memory + /memory).
  Processed into `CONTEXT_MANAGEMENT.md` / `SESSIONS.md` / `MEMORY_DESIGN.md`. **All
  fresh — none of it dogfooded yet** (see retro questions below).

## Tools
- 2026-07-04: dogfood D1 first finding — 2 of 3 real llm-kaki tasks (survey data
  sources, read a blog URL) blocked on missing web access. → web_fetch pulled
  forward from Jul 10, shipped same day (gated; stdlib fetch + HTML→text, 50K cap).
- 2026-07-04: web_fetch upgraded to CC's documented design same day (user asked
  "对标CC?" — the simple version wasn't): url+prompt → small-model extraction
  ("lossy by design"; 13.3K-char page → 2K answer in live test), 15-min cache,
  cross-host-redirect notice, http→https. Divergence left: per-DOMAIN permission
  granularity (CC prompts per new domain; minicc gates per call).
- 2026-07-04: web_search decision SETTLED by official tools-reference: CC's
  WebSearch runs on Anthropic's server-side web search backend ("not
  configurable") → server-side web_search tool IS the CC-faithful choice for minicc.
- 2026-07-04: web_search shipped (server-side web_search_20250305, max_uses 8;
  pause_turn handled in agent_loop; $10/1k tracked in /cost; settings opt-out).
  Live-verified incl. the encrypted_content round-trip. Task 1 (survey data
  sources) now fully unblocked → dogfood proper starts.

- 2026-07-09: shipped **hooks core** — CC-faithful command hooks for the 3 events with
  a real minicc surface (PreToolUse: block/allow/ask/updatedInput; PostToolUse:
  additionalContext/updatedToolOutput/block-feedback; UserPromptSubmit: block+context).
  CC's exact settings.json schema + stdin/exit-code/JSON contract, so a CC command hook
  drops in (matchers use minicc's lowercase tool names). YAGNI-cut the other ~27 events
  (absent-infra). Stop hook deferred to next commit (changes loop control flow; RALPH's
  gate). `minicc/hooks.py` + agent/cli wiring; 19 tests + HOOKS.md. Watch in dogfood:
  is the deterministic PostToolUse-lint a better verify signal than the soft stance?
- 2026-07-09 (same day, follow-up): **lifecycle hooks** — PreCompact (blockable,
  matcher manual|auto, `compact_reason` payload — re-fetched the official reference
  for the exact stdin fields), PostCompact (notify-only), SessionStart
  (startup|resume|clear; additionalContext → session-context layer), SessionEnd
  (clear|prompt_input_exit; informational per CC). Ordering call: user sequenced
  lifecycle BEFORE Stop — right by risk (none of these touch loop control flow;
  Stop becomes the single control-flow commit), vs my leverage-first ordering.
  +6 wiring tests (161 total green).
- 2026-07-10: shipped **Stop hook** — the deterministic turn-end gate (verify-work's
  enforced tier; RALPH's "done" primitive). block → reason fed back as a user
  message, loop continues; continue:false overrides a block; additionalContext
  sans block trails into the conversation; cap = 8 consecutive blocks (from
  best-practices — the hooks reference documents NO cap and NO stop_hook_active
  field; deliberately did not restore the latter from stale training-data memory).
  Main session only (SubagentStop unwired). +5 loop tests (166 green). Hooks
  feature COMPLETE → next: dogfood (llm-wiki worktree twin).

- 2026-07-16: shipped **skills** — CC's SKILL.md contract from the official
  docs (fetched same day, full page): personal/project discovery with ancestor
  walk, frontmatter (shallow hand-rolled YAML; malformed → body with empty
  metadata, CC's documented failure mode), /name + `skill` tool (listing rides
  the tool description), $ARGUMENTS/$N/named/${CLAUDE_*} substitutions with
  CC's escape semantics, !`cmd`/```! shell preprocessing (single pass, subst
  first, disableSkillShellExecution honored), allowed-tools grants scoped to
  next-user-prompt, re-invoke dedup note. Cuts ledger in SKILLS.md (fork,
  plugins, watchers — listing is session-static for cache economics). +21
  tests (224 green). Watch in dogfood: does the model invoke a matching skill
  unprompted, and does the static listing staleness ever bite?

- 2026-07-16 (same day, follow-up): **system-reminder injection** — user asked
  why CC's hot updates don't bust prompt caching; digging in exposed that my
  skills-listing placement (tool description, session-static) matched CC's
  LEGACY layout (SLASH_COMMAND_TOOL_CHAR_BUDGET is the fossil). Current CC
  injects claudeMd/memory/date/skill-listing as <system-reminder> blocks in
  the MESSAGE STREAM (append-side = cache-free updates; verified: live CC
  session observation + CC's own tool descriptions + B/C transcripts carrying
  zero reminders — they're re-derived, never persisted). Rebuilt to match:
  reminders.py (inject on first prompt / on change / on loss-from-history —
  self-healing via history scan) + watch.py (generic polling primitive, built
  for agent-teams reuse); CLAUDE.md + MEMORY.md left the prefix-cache layers
  entirely (3 of 4 breakpoints used now, one spare); date moved to
  # currentDate (a date in a cached prefix goes stale at midnight). Side
  benefit: memory writes reach context mid-session, not just at /clear.
  +10 tests (233 green). Lesson for the honesty ledger: I presented "listing
  rides the tool description" as CC fact when it was an unverified assumption
  — the user's "为什么CC热更新不会打乱caching" question caught it.

- 2026-07-16 (late, corrections): re-fetching CC's memory doc for the docs/
  update caught TWO fidelity errors in the same-day reminder work: (1) I had
  claude_md/MEMORY.md hot-reloading on mtime change — CC loads them "at the
  start of every conversation" and re-injects only after loss (compact/clear);
  only SKILLS have documented live change detection. Reverted to loss-only
  re-injection (constant snapshot = fire-once latch). (2) minicc truncated
  CLAUDE.md at 200 lines/25KB "per CC's limits" — the official page says
  "CLAUDE.md files are loaded in full regardless of length; this limit applies
  only to MEMORY.md". Truncation removed. Bonus: the doc states the delivery
  placement outright ("delivered as a user message after the system prompt")
  — the three-way inference from earlier today is now doc-confirmed. Ledger
  lesson repeated: extrapolating one documented behavior (skills live-reload)
  to a neighboring feature (CLAUDE.md) is the same class of error as the
  half-grep. Worse (user caught the contradiction between the shipped commit
  and this correction): the refuting fact was ALREADY in the repo's own
  lighthouse — docs/CC_CONTEXT_MANAGEMENT.md (mtime Jul 10, six days before
  the reminder work) says CLAUDE.md edits "don't apply until restart//clear —
  CC chooses ignore" — and I wrote the hot-reload without grepping it. New
  gate: a CC-parity behavior claim needs a cited source (official doc /
  observation / probe) BEFORE implementation — grep docs/ lighthouses first;
  no source = don't build it, record it as an assumption. 232 green.
  - Full pre-commit audit (user-ordered, no-memory rules): 16 skills-doc +
    5 memory-doc quotes re-verified verbatim; both commit trees rebuilt via
    git archive and rerun (224/233 exact); 8 doc examples executed black-box
    — all pass. Two live CC CLI probes: (a) confirmed the claudeMd
    "(project instructions, checked into the codebase)" label — the last
    training-memory item, now observed; (b) a $0-only skill got NO
    "ARGUMENTS:" append → minicc's any-placeholder-consumes reading matches
    CC's implementation over its doc's letter. Probe bonus shipped same
    audit: user-side skill expansion envelope (<command-message>/<command-
    name>/<command-args> + "Base directory for this skill:") and the
    single-newline reminder byte format.

- 2026-07-05: shipped **verify-work stance** (system-prompt "Verify your work"
  section: run tests/lint after editing, fix before reporting done). Source: it's
  CLAUDE_CODE_DESIGN.md's flagged "biggest single opportunity" + RALPH precondition,
  not a schedule item — surfaced ahead of plan mode because llm-wiki dogfood will
  vote on it immediately. Soft prior only; eval probes G1/G2 added. Watch in dogfood:
  does the model actually run pytest after editing wiki modules, or still claim done?

## Dogfood — the /init comparison experiment (llm-kaki, R1–R3, 2026-07-11 → 14)

Full records live in Obsidian (`Start/my-mini-cc/`: 三臂对比记录 + 改进建议);
this is the repo-side condensate. Setup: A = minicc+API, B = CC CLI, C = CC CLI
Fable 5, same base commit, /init in three worktrees.

- **R1/R2** (2-arm): B won structure; A won gotcha depth; verdicts flipped once
  commands were LIVE-RUN — every point B lost traced to commands it never ran.
  Fixes shipped, then hard-discarded for a clean 3-arm rerun (stash was skipped;
  lesson: archive before reverting).
- **R3** (3-arm, clean baseline): A=20 calls (18 bash-cat, ≈18 prompts, 0
  verification, 5 output errors); B=40 calls (32 Read, 0 verification, 3 errors,
  344s, 2.02M cache-read (init-span; whole-session figure was inflated)); C=15 batched calls, 192s, 695K cache-read, **2
  verification actions** (pytest before documenting — its own words: "Let me
  verify the test command works before documenting it" — and git check-ignore),
  59-line output, fewest errors. C's verified claims were all correct; its
  unverified ones were its only errors (ruff; the dev.sh-reload item was later re-verified substantially TRUE — reload=True lives in the services' uvicorn.run — my grep had covered only dev.sh: the grading rule violated by its own author, again).
- **Recovered CC's official /init prompt verbatim** from B's transcript —
  now the skeleton of minicc's /init.
- **Grading-method lessons** (recorded after two self-corrections): execution
  counts as verification, grep does not (a dev.sh short-name misjudgment); read
  the process transcript before judging the result (two C claims initially
  misfiled as fabrications were verified in-process).
- **Rebuild (2026-07-14, this working tree)**: /init = CC official skeleton +
  verify-before-write + quantifier-check + parallel-batch hint; read-only bash
  carve-out + safe redirects; `bash(prefix *)` allow rules + `always`;
  phantom-decline fix (stdin flush + empty-reprompt); adaptive thinking (both CC
  arms ran with thinking on — minicc bare was an uncontrolled variable);
  transcript ts+usage (CC-parity observability; A's timing/cost columns were
  blank in the analysis); two-layer bash steering; ancestor CLAUDE.md walk;
  recent-commits env block. 194 tests green.

- **R4 follow-up shipped (2026-07-15 晚): file-freshness contract + post-edit
  snippet** — CC's read-before-edit + modified-since-read rejection (its exact
  observed error wording) via a session-scoped path→mtime registry;
  edit_file/write_file gate on it, read_file records, own writes stay fresh,
  /clear resets. edit_file now returns the edited region with line numbers
  (±2 context, big inserts elided) — the lightweight version of CC's
  "file state is current in your context" mechanism, discovered in C's R4
  transcript. +8 tests (203 green).

- **2026-07-17: survey-driven A-group landed** — user reset the two skill
  commits and ordered a fresh full survey before recommitting. Three probes
  closed the open questions: /init persists as tags-record + isMeta-record
  (B transcript line 4/5 — the expansion IS persisted, unlike reminders);
  Skill tool_result carries the base-dir header on the model path too; the
  listing lives in a post-system block, not the tool description (first probe
  answered NONE — single negative probes lie; triangulate). Shipped: two-
  record expansion with sessions `meta` flag (/init included), base-dir on
  both paths; SKILLS.md superseded by SKILL_DESIGN.md (survey + evidence
  classes + next-steps ledger). E2E re-verified live (record shapes + zero
  reminder persistence + model received injection). 233 green.

## Open questions for retro
- [ ] verify-work: does the model run tests/lint unprompted after edits now, or
      does the stance get ignored under task pressure? (the whole point of shipping it)
- [ ] Does the model follow codebase conventions reliably on real tasks, or is
      the F1 variance pattern common?
- [ ] When edit_file genuinely fails (actual not-found, not multi-match), does
      the model recover sensibly?
- [ ] Is the bash-fallback rate acceptable, or does it escalate to bash when
      grep/glob would do?
- [ ] Memory: does the model write memory unprompted, and is what it saves
      actually useful next session? (the what-to-persist policy wants data)
- [ ] Memory: is the write-approval prompt too much friction mid-task?
- [ ] L3 keep 4→3: does re-read churn go up noticeably?
- [ ] Does the 9-section summary preserve enough to continue work cleanly after
      an auto-compact? (don't force it — if it never fires in real use, that's
      data too)

## QA items (directed tests, NOT dogfood — don't let these shape real-work tasks)
- [ ] Reactive-429: can't be honestly provoked — if a real rate-limit hits, jot
      the `/context` numbers.
- [ ] Resume across a compaction (transcript replay): scripted end-to-end check
      (drive a session past a compact, quit, `--continue`, continue working).
      ~10 minutes, run separately from dogfood.
