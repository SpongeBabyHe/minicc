# Auto-memory — focused note

Cross-session memory the model writes and reads back, so learnings survive when the
conversation doesn't. minicc's take on Claude Code's *auto memory*, distilled from a
survey of the official docs (Sources below). (The on-disk index the model maintains
is *also* called `MEMORY.md`, but lives under `~/.minicc/`, not here.)

## The idea

`CLAUDE.md` is *read-only* project memory the user writes. Auto-memory is the other
half: the **model** records durable findings as it works and reads them back next
session — build commands, decisions, preferences, fixes, project facts. It realizes
context-engineering's *structured note-taking*: persist outside the window, recall on
demand.

Two layers, mirroring CC:

- **`MEMORY.md` index** — a concise index, **loaded into every session** (first 200
  lines / 25 KB), so the model always sees *what it knows*.
- **Topic files** — one fact each, **read on demand** with the memory tool, so detail
  stays out of the window until needed.

## Storage — machine-local, per-repo

`~/.minicc/projects/<repo-key>/memory/`, where `<repo-key>` is the git toplevel path
(`/`→`-`), or the cwd outside a repo. So all worktrees of a repo share one store, and
it's never committed (it's under `$HOME`, not the project). Mirrors CC's
`~/.claude/projects/<repo>/memory/`. Set a different path later if needed.

## The `memory` tool

One tool over a `/memories` prefix this maps onto the real store, with
path-traversal protection (reject anything resolving outside the root). Three
commands (a subset of CC's GA `memory_20250818`, wire-compatible for a later upgrade):

| command | what |
|---|---|
| `view` | a file (line-numbered) or the directory listing |
| `create` | write (or overwrite) a file |
| `str_replace` | replace one **unique** occurrence (omit `new_str` to delete) |

Topic files use CC's shape (the tool description steers this):

```
---
name: <kebab-slug>
description: <one line — used to judge recall relevance>
metadata:
  type: user | feedback | project | reference
---
<the fact; link related memories with [[their-name]]>
```

`user` = how the person works · `feedback` = corrections / confirmed approaches ·
`project` = time-bound work facts · `reference` = external pointers. Keep `MEMORY.md`
a tight index; add a one-line pointer there when you create a topic file.

## How it wires into the rest

- **Cache layer** — the `MEMORY.md` index is **merged into the project-context block**
  next to CLAUDE.md (one breakpoint), so it rides the project cache and reloads with
  it. See [CONTEXT_MANAGEMENT.md](CONTEXT_MANAGEMENT.md) § L1.
- **Lifecycle** — index loaded at startup; **reloaded on `/clear`** (memory *persists*
  across `/clear` — only the in-context snapshot reloads); survives compaction the
  same way (re-read from disk), matching CC's "project memory survives compaction."
- **Gating** — writes (`create`/`str_replace`) go through the permission prompt like
  `write_file`; **`view` is ungated** so the model can always read memory. Command-aware
  gating lives in `permissions.py`.
- **Sub-agents** — memory is **not** in the read-only tool set, so a sub-agent's
  isolated context never reads or writes the main store.
- **Steering** — a line in the system prompt tells the model to record durable
  learnings (not transient task state) and keep memory organized.

## `/memory`

Browse and toggle from a session: `/memory` lists the store (path + files), `/memory
<file>` views one, `/memory on|off` toggles auto-memory for the session (off = the
index isn't injected and writes are refused).

## Status & scope

✅ Implemented (MVP): store + `memory` tool (view/create/str_replace) + path safety +
index in the cache layer + gated writes + `/memory` browse/toggle. Unit tests in
`tests/test_memory.py`.

Deferred:
- **Consolidation** (CC's "Auto Dream") — periodic merge-duplicates / prune-stale /
  keep-the-index-tight. MVP writes inline; add an idle or `/memory consolidate` pass.
- The full 6-command GA tool (`insert`/`delete`/`rename`) — the 3-command subset covers
  the MVP; upgrade if the model wants the rest.
- Persisting the on/off toggle across sessions (currently session-scoped).

## Alignment (vs Claude Code)

**Following CC**: two-layer index + lazy topic files; 200-line/25 KB index load;
machine-local per-repo store; the topic-file frontmatter format + `[[links]]`; model
decides what's worth saving; survives `/clear`/compaction by disk reload. **minicc's
own**: the 3-command tool subset (vs GA's 6); gated writes (CC's tool auto-injects a
protocol prompt instead); `~/.minicc/` path. **Not baked in**: CC's exact save-decision
heuristic and Auto-Dream cadence are model-/flag-internal — minicc leans on steering +
the model's judgment, tuned from dogfood.

## Sources

- [Memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) — the GA `memory_20250818` tool: commands, `/memories`, path safety, the auto-injected protocol prompt.
- [How Claude remembers your project](https://code.claude.com/docs/en/memory) — CC auto-memory: `MEMORY.md` index + lazy topic files, `~/.claude/projects/<repo>/memory/`, on-by-default, `/memory`.
- [Explore the context window](https://code.claude.com/docs/en/context-window) — where memory loads; the "what survives compaction" table (auto memory re-injected from disk).
- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — structured note-taking / just-in-time retrieval.

The topic-file frontmatter format is confirmed against CC's own on-disk memory store;
the "Auto Dream" consolidation cadence is community-observed, not officially documented.
