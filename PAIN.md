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
