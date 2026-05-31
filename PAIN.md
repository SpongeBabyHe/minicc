# Known issues / observations

## M5 v1 eval

### Investigated, not blocking

- **D2 test is structurally broken.** Model recognizes multi-match
  risk before calling edit_file and bypasses to write_file. Sound
  behavior, but means edit_file's multi-match error recovery path
  remains unverified by D2. To test it directly, would need to call
  edit_file manually with a non-existent old_text and observe model
  response. (Verified in 20260526_145725_v1.log)

- **F1 behavior varies across runs.** First run (20260526_142657):
  model skipped reading existing tool as template, wrote ls.py using
  os.listdir (inconsistent with codebase pathlib style). Second run
  (20260526_145725): read glob.py first, used pathlib (consistent).
  Same prompt, different outcomes — sampling variance. Not a prompt
  bug per se; consider adding "read existing files for style" hint
  if this becomes a recurring issue in M7 dogfood.

### To investigate during M7 dogfood

- [ ] Does the model follow codebase conventions reliably on real
      tasks, or is the F1 variance pattern common?
- [ ] When edit_file genuinely fails (not multi-match — actual
      not-found), does the model recover sensibly?
- [ ] Is the bash fallback rate acceptable, or does the model
      escalate to bash too often when grep/glob would suffice?

## M6–M7 dogfood

### 2026-05-28 - CJK language drift

With English-only system prompt, the model may switch from Chinese to
Japanese mid-response when handling technical content (because English
code identifiers act as "language reset points" and kanji/hanzi share
token space). Fix: anchor response language explicitly in system prompt.
Observed in my-claude-app first, but minicc has the same gap.

### 2026-05-29 — rate limit / history [紧急]
Query：这个项目当时停在哪一步？我想优化RAG的效率， 有哪些方案，先与我讨论方案

Dogfood 不到 1 小时撞 rate limit 两次，第二次"等 1 分钟"无效（单次请求就超
450K）。暴露根本设计缺陷：minicc 没有 history 管理，长任务必然撞墙。

### 2026-05-31 — verification / glob truncation

Model concludes "no file > 2000 lines" after reading only 3–5 files. Repeated
2x. Glob output showed `[+2736 more chars]` truncation marker — model ignored
the incompleteness signal.

Root cause:

- LLM bias toward confident-brief answers
- minicc prompt actively rewards brevity ("act when you have enough information")
- No prompt guidance about verification claims requiring exhaustive evidence

Possible fixes (v0.2 prompt iteration):

- Add "When verifying claims..." section to system prompt
- Add file_stats tool (v0.3)

User workaround: state exhaustiveness requirement explicitly in query.
