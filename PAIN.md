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
- 2026-06-15: L4/L5 implemented & validated. Details (cut-point fix, survey-task
  churn, budget invariant, validation, v0.3 gaps) synthesized in
  `CONTEXT_MANAGEMENT.md` → "Dogfood lessons & validation".

## Open questions for retro
- [ ] Does the model follow codebase conventions reliably on real tasks, or is
      the F1 variance pattern common?
- [ ] When edit_file genuinely fails (actual not-found, not multi-match), does
      the model recover sensibly?
- [ ] Is the bash-fallback rate acceptable, or does it escalate to bash when
      grep/glob would do?
