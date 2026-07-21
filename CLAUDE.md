# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and minicc when
working with code in this repository.

## What minicc is

A from-scratch Python reimplementation of Claude Code's core, built for
**maximum 1:1 fidelity to CC's behavior and contracts** (tool schemas, prompts,
mechanisms) — not to CC's file layout. CC is TypeScript; its file conventions
(PascalCase, dir-per-command, React components) do NOT port. minicc stays
idiomatic Python: lowercase snake_case modules, flat package + `tools/`
subpackage. Copy CC's *concepts and vocabulary*, implement them the Python way.

## Module naming (do not let this drift)

The "agent" and "task" words each map to ONE concept — keep them separated:

| Module | Concept | CC's name |
|---|---|---|
| `query_engine.py` | the turn loop (`agent_loop`: LLM↔tool round-trips) | `QueryEngine` |
| `agents.py` | sub-agent role definitions (`.minicc/agents/*.md`) | agent defs |
| `tools/agent.py` | the sub-agent spawn tool (name `agent`) | `Agent` (was `Task`) |
| `tasks.py` + `tools/task_*.py` | the coordination task LIST (deps, owner) | `Task*` |
| `cli.py` | the REPL + entry (`main`) + slash commands | `App` + `cli/` + `commands/` |
| `llm.py` | the API client (request assembly, cache layers, streaming) | `services/api/` |
| `compact.py` | context management: budgets/eviction/compaction + per-conversation `ContextState` | `services/compact/` |

**Reserved** (map to real future CC components — don't repurpose):
`runner.py` = the process/session spawner (CC `sessionRunner`; future
teammate/background process registry); `loop.py` = the future `/loop` feature.

CC also has a runtime **task registry** (7 TaskType / 5 TaskStatus /
`isTerminalTaskStatus`, behind `TaskOutput`/`TaskStop`) that is DISTINCT from the
coordinator task list in `tasks.py` — deferred to the background/teams tiers.
See docs/CC_AGENTS_COORDINATOR_DESIGN.md §2b.

## Working conventions

- Run tests with `python -m pytest -q` (fast; ~260 tests). Everything ships
  with tests; keep the suite green.
- 1:1 claims need a cited source (official doc / live-harness observation /
  reverse-engineering) BEFORE implementing — never extrapolate one layer's
  behavior to another (tool schema ≠ data model; skills' live-reload ≠
  CLAUDE.md's). Grep docs/ lighthouses first.
- docs/ is gitignored research lighthouses; root `*.md` are tracked feature
  notes; PAIN.md is the raw dogfood scratchpad.
