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


### 2026-06-14 — L3 eviction + re-read works as designed

**Observation:** With TOKEN_BUDGET=2000, RECENT_TOOL_RESULTS_KEEP=4, dogfood on llm-kaki-heman.
Turn 1 read 4 files. Turn 2 asked about the first file's first import — model
RE-READ main.py instead of answering from memory. This confirms its
tool_result was evicted and the model gracefully re-called read_file on
seeing EVICTED_MARKER.

**Significance:** Validates the L3 design's core bet — the model treats
"[content omitted; re-call if needed]" as a signal to re-fetch, not as
missing data to confabulate around. Counters the earlier worry (2026-05-31
entry) about confident-but-wrong behavior.

**Cost tradeoff:** Re-reading means extra tool calls + tokens. Eviction
trades request size for occasional re-fetches. Acceptable, but worth watching
how often re-reads happen in practice. If it is too often, means RECENT_TOOL_RESULTS_KEEP need to be larger.

### 2026-06-14 — L3 eviction: model explains the mechanism unprompted
**Status:** 🟢 validated  **Tags:** context

**Observation:** With TOKEN_BUDGET=1000, KEEP=2, /context showed 6/8
tool_results evicted. When asked "why don't you remember the content but
read it again?", the model accurately explained: it saw the
"[content omitted ... re-call the tool if needed]" marker, understood the
content was stripped to save space, and chose to re-read rather than risk
misremembering.

**Significance:** The EVICTED_MARKER wording directly shaped correct behavior.
Model treats it as an instruction, not as a void to confabulate around.
Strongest validation yet of the L3 design bet.

**Also confirmed:** /context surfaces eviction state (6 evicted) that would
otherwise be invisible — validates L6a's purpose.

**Open:** estimate stayed at 2994 > 1000 budget even after eviction, because
the 2 retained recent tool_results are themselves large. Confirms L3 alone
can't bound context when recent blocks are big — L4 (compact) needed.


### L1 caches stable prefix only, not conversation history
**Status:** ⏭️ v0.3 candidate  **Tags:** context, cost

minicc L1 caches system+tools+project but NOT messages, to avoid L3 eviction
thrashing the cache. CC instead caches the full conversation (the biggest part
of the request) and accepts that compact/eviction invalidates it occasionally —
likely the better cost tradeoff. v0.3: add an advancing cache_control breakpoint
on recent messages, accept occasional eviction busts.


### 2026-06-14 — CLAUDE.md injection validated; redundant read only on meta-questions
**Status:** 🟢 validated  **Tags:** prompt, context

**Observation:** Two tests:
- Asked "what conventions does this project follow?" → model read_file'd
  CLAUDE.md (redundant; content was already injected).
- Asked to write path-handling code → model used pathlib + explicitly noted
  "no os.path needed", WITHOUT reading CLAUDE.md.

**Conclusion:** L1 injection works AND shapes behavior. The "no os.path needed"
comment proves the model absorbed the "never os.path" rule from injected
context. Redundant read happens only on meta-questions about the project,
not during actual work — low impact.