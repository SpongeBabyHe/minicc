# SKILL_DESIGN.md — skills: survey, design, evidence

minicc's skills system is a 1:1 copy of Claude Code's, built from a full
survey of CC's mechanism (2026-07-16/17). This doc is the survey result, the
design mapping, and the ledger of everything deliberately not copied. Replaces
the earlier SKILLS.md.

## Evidence base

Every behavior below carries one of these source classes — nothing rests on
training memory:

| Class | Source |
|---|---|
| doc | Official skills page (code.claude.com/docs/en/skills, fetched in full; 16 load-bearing rules quote-verified) + memory page (fetched twice) |
| probe | Live CC CLI runs (2026-07-16/17): claudeMd reminder format, user-path expansion, Skill tool_result, listing location, `$0`-args behavior |
| observed | A live CC session's own tool descriptions and reminders; B/C experiment transcripts (incl. the `isMeta` record shape and the zero-reminder property) |

## How CC does it (survey)

**Storage & discovery.** `enterprise → ~/.claude/skills/<name>/SKILL.md
(personal) → .claude/skills/<name>/SKILL.md (project) → plugins`. Broader
levels win name clashes (doc: "personal overrides project"). Project skills
load from cwd AND every parent up to the repo root; nested dirs below cwd load
on demand with `dir:name` qualified names. The command word is the DIRECTORY
name; frontmatter `name` is display-only. Legacy `.claude/commands/*.md` still
works. Live change detection: add/edit/remove takes effect within the session.

**Context delivery (progressive disclosure).** Only the LISTING is always in
context: header `The following skills are available for use with the Skill
tool:`, entries `- name: description` (probe-verbatim). It is NOT in the Skill
tool's description (probed — the tool description just points at it; the
`SLASH_COMMAND_TOOL_CHAR_BUDGET` env var is the fossil of the old
embed-in-tool-description layout). Per-entry text (description + when_to_use)
caps at 1,536 chars; the whole listing budgets at 1% of the context window,
evicting least-used descriptions first. The BODY enters context only on
invocation and stays for the session; a re-invocation with identical rendered
content gets a short "already loaded" note instead of a second copy. After
compaction, invoked bodies re-attach (5K/skill, 25K total, most recent first).

**Two invocation paths (probed byte shapes).**

User path `/name args` — the transcript carries TWO records:

```
record 1 (user):            <command-message>name</command-message>
                            <command-name>/name</command-name>
                            <command-args>args</command-args>   ← omitted when no args
record 2 (user, isMeta):    Base directory for this skill: <dir>
                            ␣
                            <rendered body>
```

Model path — Skill tool `{skill, args}`; the tool_result is the same
expansion shape WITHOUT tags: `Base directory for this skill: <dir>` + blank
line + rendered body. Ungated by default; `Skill(name)` / `Skill(name *)`
permission rules can restrict. The tool description text is fixed (identical
across CC surfaces, quote-verified).

**Rendering pipeline.** Substitutions first — `$ARGUMENTS` (raw string as
typed), `$ARGUMENTS[N]`/`$N` (shell-quoted), `$name` (declared in
`arguments:`), `${CLAUDE_SESSION_ID}`, `${CLAUDE_EFFORT}`,
`${CLAUDE_SKILL_DIR}`, `${CLAUDE_PROJECT_DIR}` (body AND allowed-tools).
Escapes: exactly one `\` → literal (backslash consumed); `\\` → both stay AND
the token expands. Then shell preprocessing: `` !`cmd` `` (line start / after
whitespace only) and ```` ```! ```` blocks run before the model sees anything;
single pass, output never re-scanned; `disableSkillShellExecution` swaps each
for `[shell command execution disabled by policy]`. Args fallback: the doc
keys `ARGUMENTS: <value>` appending on `$ARGUMENTS` being absent, but a probe
($0-only skill, args passed) showed NO append — any consumed placeholder
suppresses it. Malformed frontmatter YAML → body loads with empty metadata.

**While a skill is active.** `allowed-tools` grants listed tools without
prompting (pool unchanged); `disallowed-tools` removes tools from the pool;
the window for both is pinned by the doc: "clears when you send your next
message". `model`/`effort` override for the turn; `context: fork` runs the
body as a subagent prompt; `paths:` gates auto-activation; per-skill `hooks`.

**Config surface.** `skillOverrides` (on / name-only / user-invocable-only /
off), `disableBundledSkills`, `skillListingBudgetFraction`,
`skillListingMaxDescChars`, `disableSkillShellExecution`.

## minicc's implementation map

Locations mirror CC under minicc's config root (the hooks precedent — CC's
schema, `.minicc/` paths; user decision 2026-07-17: no `.claude/` compat read):

| Level    | Path                                                | Wins name clashes |
|----------|-----------------------------------------------------|-------------------|
| Personal | `~/.minicc/skills/<name>/SKILL.md`                  | over project      |
| Project  | `<cwd & ancestors>/.minicc/skills/<name>/SKILL.md`  | closest-to-cwd within level (interpretation — CC doesn't specify ancestor clashes) |

A CC-authored SKILL.md body + frontmatter drop in unchanged — caveat:
`allowed-tools` entries naming CC tools (`Read`, `Grep`) don't map to minicc's
lowercase names and no-op silently; `bash(...)` rules do map,
case-insensitively.

- **User path**: unknown slash commands fall through to skill lookup
  (built-ins win a clash). The turn becomes CC's two-message expansion: a
  command-tags user message + the expansion user message (base-dir header +
  rendered body); the transcript marks the second record `meta: true` (CC's
  `isMeta`). `/init` uses the same shape. UserPromptSubmit hooks fire for
  expansion turns with the expanded content as `prompt`; built-in
  commands (`/help`, `/compact`, …) skip hooks, as before.
- **Model path**: the `skill` tool. Description is CC's own text ported
  verbatim minus the plugin / directory-scope / subagent sentences (absent
  infra). tool_result = base-dir header + rendered body; identical re-invoke
  → "already loaded" note (its wording is ours — CC's exact note text
  unobserved). Ungated.
- **Listing**: injected as a `<system-reminder>` block by `reminders.py` on
  the first prompt, re-injected when the skill set changes (`watch.py` mtime
  poll at prompt boundaries — the user-sanctioned stand-in for CC's file
  watchers). Entries `- name: description`; `argument-hint` is
  autocomplete-scoped per the doc, so it surfaces in `/help` instead.
- **Rendering**: the full CC pipeline above, including the probe-decided
  any-placeholder-consumes rule. Hand-rolled shallow YAML parser (scalars,
  inline/block lists, quotes, bools — no pyyaml); CC's documented
  malformed-YAML behavior makes forgiving parsing the faithful choice.
  Shell timeout 60s (minicc's own constant; CC documents none), stderr
  appended to output (CC unobserved).
- **allowed-tools**: grants die on the next user prompt (CC's window);
  `bash(...)` shapes reuse the persisted-rule compiler; `${CLAUDE_*_DIR}`
  resolve inside rules. Grants print when applied — minicc UX (kept by user
  decision; PERMISSIONS.md visible-trust principle).
- **Body freshness**: lookup rescans disk per invocation, so body edits apply
  on next use with no re-injection (equivalent to CC's live detection at REPL
  cadence).

Invocation control (frontmatter), same table as CC:

| Frontmatter                      | User | Model | Description in context |
|----------------------------------|------|-------|------------------------|
| (default)                        | yes  | yes   | yes                    |
| `disable-model-invocation: true` | yes  | no    | no (fully hidden)      |
| `user-invocable: false`          | no   | yes   | yes                    |

## What the survey found beyond skills

The skills work exposed CC's whole volatile-context architecture; these
reshaped minicc outside this feature (details: CONTEXT_MANAGEMENT.md,
MEMORY_DESIGN.md, PAIN.md ledger):

1. claudeMd/memory/date delivery: `<system-reminder>` blocks inside user
   turns — the memory doc says it outright ("delivered as a user message
   after the system prompt"). Formats probe-verbatim.
2. CC transcripts persist at three levels: normal messages (full), slash
   expansions (two records, second `isMeta`), reminders (never — re-derived
   at request time).
3. CLAUDE.md loads IN FULL; the 200-line/25KB cap is MEMORY.md-only (fixed a
   minicc truncation bug).
4. CLAUDE.md has NO live reload — start-of-conversation + re-inject-on-loss;
   live detection is skills-only (reverted a minicc overreach).
5. Method lessons: doc wording vs implementation can differ (probe decides);
   a single negative probe isn't conclusive (triangulate); grep your own
   research lighthouses before building (the hot-reload failure's root).

## Cuts ledger (absent infra — revisit when the infra exists)

| CC feature | Why cut |
|---|---|
| Plugins / enterprise / bundled skills | no plugin or managed-settings infra |
| Nested `.claude/skills` below cwd + `dir:name` qualified names | no monorepo need yet |
| File watchers | `watch.py` polling at prompt boundaries — same observable behavior at REPL cadence (user-sanctioned) |
| `context: fork` + `agent:` | minicc's sub-agent is a read-only Haiku explorer — wrong substrate for task skills |
| Per-skill `hooks`, `model`, `effort`, `paths`, `shell: powershell` | no per-turn model/effort switching; hooks are global; darwin only |
| `disallowed-tools` | needs tool-pool mutation mid-session (cache churn), no use case yet |
| `skillOverrides`, listing budget fraction + least-used eviction | no usage-frequency tracking; per-entry 1,536 cap kept |
| Skill stacking (`/a /b args`), compaction re-attachment budget | rare; compactor doesn't special-case skills yet |
| `${CLAUDE_EFFORT}` | minicc has no effort levels — left literal |
| `Skill(name)` permission rules | skill tool is ungated; add when a deny need appears |
| Legacy `commands/` dir | minicc never had one — nothing to honor |

## Next steps

**Provenance upgrades (no behavior change):**
- The `skill` tool description's dropped sentences are now CLI-probe-confirmed
  parts of CC's original — the port comment should cite the probe, and the
  drops stay (describing absent capabilities would misdirect the model).
- The listing's `<system-reminder>` wrapper is uncertain: a CLI probe's model
  perceived the listing as a system block after the system prompt, NOT a
  reminder-tagged block (while CC's own tool description calls it "a
  system-reminder listing"). Kept wrapped; record the uncertainty in
  reminders.py when next touched.

**Settled deviations (user decisions, 2026-07-17 — do not revisit without a
new ruling):** `.minicc/` config root with no `.claude/` compat read; the four
minicc UX prints (grant line, startup skills line, `skill:` line, /help
listing) stay.

**Open / unverifiable (blocked on observability, not effort):**
- CC's exact wording for the "already loaded" dedup note and the
  disable-model-invocation error.
- Separator between multiple `Contents of …` blocks in the claudeMd reminder
  (no multi-file observation; symmetric choice implemented).
- Whether reminders attach before or after command tags when both share a
  turn (reminder-first implemented, matching session-opening shape).
- Ancestor-level name-clash precedence within the project level (closest-wins
  is an interpretation).

## Files

`minicc/skills.py` (parse/discover/render/expand/grants),
`minicc/tools/skill.py` (the tool; CC-verbatim description),
`minicc/reminders.py` (listing + claudeMd injection), `minicc/watch.py`
(polling primitive), `minicc/sessions.py` (`meta` records),
`minicc/permissions/authorization.py` (turn-scoped grants), `minicc/config.py`
(`skill_shell_disabled`), `minicc/cli.py` (two-message expansion, `/help`,
injection point), `tests/test_skills.py`, `tests/test_reminders.py`,
`tests/test_sessions.py`.
