# Tool schemas — descriptions, contracts, and this pass's fixes

How minicc writes tool `description`s and `input_schema`s, and the rationale for
the schema-correctness pass that produced this doc. Grounded in Anthropic's own
tool-use guidance (sources at the bottom), not assertion.

---

## The one idea: a description is a soft prior, not a guarantee

A tool `description` reliably steers **which tool** the model picks and **when**,
but it cannot *guarantee* anything about the call. Anthropic's docs draw the line
explicitly — there is a ladder of mechanisms, and each tier guarantees strictly
more than the one above it:

| Tier | Mechanism | What it can guarantee |
|---|---|---|
| 1. **Steer** | `description` + system prompt | nothing — "small refinements…can yield dramatic improvements," but the model "[doesn't] always say what they mean." Routing only. |
| 2. **Shape** | `tool_choice`, `strict: true` | that *a* tool is called, and that inputs match the **JSON schema** (types/required/enums). |
| 3. **Enforce** | the **handler code** + an `is_error` result | the only tier that can enforce a **value/state invariant** (e.g. "old_text occurs exactly once *in this file*") and teach recovery. |

The practical rule that falls out:

> **State the contract in the description; enforce it in code; teach recovery in
> the error string.** A description that *claims* an invariant the handler doesn't
> check is a latent bug — the model trusts it and is silently wrong.

This pass found three descriptions that claimed behavior the code didn't deliver,
and fixed the code (the description was describing the *safer* contract).

---

## The detail dial: rich on semantics, zero on mechanics

Anthropic: *"Provide extremely detailed descriptions. This is by far the most
important factor in tool performance… Aim for at least 3–4 sentences."* The detail
should be the **caller's decision model** — what it does, when (and when not) to
use it, what each parameter means, what it returns, **and what it does not** —
i.e. "describe it to a new hire." It should **not** be implementation mechanics
(exact char caps, on-disk paths, byte arithmetic): those drift from the code and
dilute the load-bearing routing clause.

Where each kind of fact belongs:

- **Routing ("when / when-not")** → the description. Highest leverage; only seen here at decision time.
- **Standing limitations ("≤100 matches per file", "killed after 120s", "no other info returned")** → the description, as a one-clause caveat. Anthropic explicitly lists "important caveats or limitations" as description content.
- **Per-run events ("THIS output was truncated at 50K")** → a just-in-time notice *in the tool's output*, generated from the real run (so it can't drift).
- **Value/state invariants** → handler code + an actionable error.
- **Cross-cutting steering shared by all tools** (prefer edit over write; search before assuming) → the system prompt, said once.

Minimalism applies to the **number of tools** and the **size of responses** — not
to per-tool semantic detail. minicc keeps 8 focused, non-overlapping tools; each
one earns a detailed description.

---

## What changed in this pass

Each row: the defect (description ≠ code, or a description that misled), and the fix.

| Tool | Defect | Fix |
|---|---|---|
| **edit_file** | Description promised `old_text` must appear **EXACTLY ONCE**, but the handler only checked for *zero* occurrences and then silently replaced the **first** of multiple matches → the model edits the wrong place believing it was unique. | Handler now counts occurrences and **rejects 0 or >1** with an actionable error ("appears N times… add surrounding lines"). Added `input_examples` (format-sensitive tool) and per-parameter descriptions. |
| **grep** | Description promised `file:line:` prefixes and "capped at 100 matches." Reality (verified): under captured (non-TTY) output `rg` emits **no line numbers** and no filename on a single file; `--max-count` is **per file**, not total; a bad regex returned a misleading `No matches.`; a silent 50K cut. | Command now passes `--line-number --with-filename` → real `file:line:` prefixes. Description states the true cap ("100 **per file**", 50K char total, noted on truncation). A `rg` error (exit ≥2) now returns `Error: …`. Added per-parameter descriptions. |
| **read_file** | Had only `limit` (head of file). `bash`'s truncation message told the model to "read_file … with **offset**/limit" — an offset the tool didn't have. | Added a 1-based `offset` parameter → windowed reads of large files; the bash promise now holds. Notice reports the exact window returned. |
| **write_file** | Returned `len(content)` labelled "**bytes**" — actually a character count (wrong for any non-ASCII content). | Reports true UTF-8 byte count (and chars). Added a prefer-`edit_file` caveat + per-parameter descriptions. |
| **bash** | Safety filter used **substring** matching: `rm -rf /` blocked the legitimate `rm -rf /tmp/x`; `sudo` blocked `echo pseudocode`; `> /dev/` blocked the ubiquitous `2>/dev/null`. The 120s timeout was undocumented. | Filter is now **anchored/word-boundaried** regex — catches `rm -rf /`, `sudo`, `shutdown`, `reboot`, `mkfs`, raw-disk writes, while allowing the legitimate forms above. The 120s cap is a standing caveat in the description **and** a just-in-time "timed out after 120s" message. |

The `task` and `todo_write` descriptions were reviewed and left unchanged — they
already match their handlers and steer well.

Tests: `tests/test_tools.py` pins each promised behavior (edit uniqueness, grep
`file:line:` prefixes, read offset/limit window, write byte count, bash filter
precision) so description and code can't drift again. Full suite: 68 passing.

---

## Conventions for future tools

1. **Description = 3–4 sentences of semantics**: what / when / when-not / params / returns / what it does *not* do. No implementation mechanics.
2. **Every non-obvious parameter gets a `description`** (Anthropic's canonical example describes every field). Self-evident names (`command`, `content`) can be terse.
3. **Invariants go in the handler**, returned as `Error: …` strings that say *how to fix the call* and, where useful, show a correct shape — never an opaque traceback.
4. **Per-run facts are emitted in the output**, not preannounced in the description.
5. **For format-sensitive inputs, add `input_examples`** (schema-validated) rather than prose alone.
6. Promote out of `bash` only to gate / enforce an invariant / render / parallelize (see CLAUDE_CODE_DESIGN.md). If none of those, it's a bash command, not a tool.

---

## Sources

- [Define tools — best practices for tool definitions](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools) — "extremely detailed descriptions… by far the most important factor"; caveats/limitations; per-parameter descriptions; `input_examples`; `tool_choice`; `strict`.
- [Tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) — "Claude determines when to call a tool based on… the tool's description"; steer via system prompt vs. *require* via `tool_choice`.
- [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) — describe-to-a-new-hire; error responses as the self-correction channel; high-signal responses; tool consolidation.
- Empirical: `rg` under captured output omits line numbers / single-file filename; `--max-count` is per file (verified in-repo).
