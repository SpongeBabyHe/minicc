# Known issues / observations

## From M5 v1 eval

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


## To investigate during M7 dogfood

- [ ] Does the model follow codebase conventions reliably on real
      tasks, or is the F1 variance pattern common?
- [ ] When edit_file genuinely fails (not multi-match — actual
      not-found), does the model recover sensibly?
- [ ] Is the bash fallback rate acceptable, or does the model
      escalate to bash too often when grep/glob would suffice?

# From M6-M7 dogfood

- **CJK language drift**: With English-only system prompt, the model
  may switch from Chinese to Japanese mid-response when handling
  technical content (because English code identifiers act as
  "language reset points" and kanji/hanzi share token space).
  Fix: anchor response language explicitly in system prompt.
  Observed in my-claude-app first, but minicc has the same gap.