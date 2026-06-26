# Subagents (Tier 1)

Run a token-heavy subtask in its **own context window** and return only a
short result to the main conversation. The main context stays clean; the
subagent's dozens of file reads never pollute it.

**Inspired by Claude Code's documented Agent tool, not a 1:1 copy.**

## Background

Some subtasks are exploration-heavy: "understand how the graph service handles
search" might read 15 files. Done in the main loop, those 15 tool_results sit in
history forever (until L3/L4 evict them), bloating every subsequent request.

A subagent runs that subtask in a **separate** agent loop with its own messages.
The parent sees only the subagent's final summary — one `tool_result`, not 15.
This is context isolation, and it's complementary to L1–L6 (which manage a single
context; subagents avoid filling it in the first place).

## Verified from Claude Code docs

(From the [tools reference][cc-tools].)
- The Agent tool "spawns a subagent with its own context window to handle a task.
  The subagent works through its task autonomously, then returns **a single text
  result** to the parent. The parent does not see the subagent's intermediate
  tool calls or outputs, only that final result."
- Subagent tool access is configurable (`tools` / `disallowedTools`).
- Subagents can run foreground or background; `maxTurns` caps how long one runs.

[cc-tools]: https://code.claude.com/docs/en/tools-reference

## Decisions

### D1. Who spawns it — a model-invoked tool ✅ (verified pattern)

Options: (A) model-invoked — expose an `agent` tool the model calls when it
judges a subtask is heavy; (B) user-invoked — a `/agent <task>` command.

**Choice: A, a `task` tool** (matches CC). The model decides to delegate, because
it's the one that knows a subtask will be exploration-heavy. Signature:
`task(description: str)` → returns the subagent's final text. (User-invoked can
be added later; not needed for v1.)

### D2. Execution model — synchronous, reuse `agent_loop` ✅ (own choice)

The subagent **is** an agent loop with a fresh `messages` list and its own system
prompt. We already added the hook for this in L1:
`agent_loop(messages, system=...)`. So:

```python
def task(description: str) -> str:
    sub_messages = [{"role": "user", "content": description}]
    agent_loop(sub_messages, system=SUBAGENT_PROMPT)   # the L1 system override hook
    return _final_text(sub_messages)
```

- **Synchronous** (parent blocks until the subagent finishes). Background/async
  is deferred — sync is far simpler and enough for v1.
- **Reuses `agent_loop`** — no second loop implementation. The `system` override
  param added in L1 was built exactly for this.

### D3. Tool access — read-only subset ✅ (own choice) — and it resolves D4

A subagent gets a **restricted, read-only tool set**: `read_file`, `glob`,
`grep`. **No** `bash` / `write_file` / `edit_file`.

Why read-only:
- Subagents are for **exploration/research** — the high-value, safe case.
- It **resolves the permission problem (D4) entirely**: read-only tools are not
  in `GATED_TOOLS`, so there are **no `confirm` prompts inside a subagent**.
  Mid-subagent permission prompts would be confusing UX ("approve what, for
  which agent?"); read-only sidesteps it.
- A subagent can't damage anything, so letting the model spawn them freely is
  safe.

Write-capable subagents (with a permission-inheritance design) are a v0.3
question, only if dogfood shows a real need.

### D4. Permissions inside a subagent — none needed (consequence of D3)

Because the subagent's tools are all read-only (ungated), `confirm` never fires
inside it. No inheritance rules, no nested prompts. **D3 makes D4 disappear** —
that's the main reason D3 is read-only.

### D5. Context isolation — parent gets only the final text ✅ (verified)

The parent's history gains exactly one `tool_result`: the subagent's final
answer. The subagent's intermediate tool calls/results live only in
`sub_messages`, which is discarded when `task()` returns. This is the whole
point — and it means the subagent's exploration **cannot** bloat parent context.

### D6. UI display — dimmed, indented subagent activity ✅ (own choice)

The user should see progress without confusing it with the main loop:
```
→ task("understand graph search flow")
  ⎿ → glob(...)            ← subagent calls, dimmed + indented
  ⎿ → read_file(...)
  ⎿ ...
  ⎿ [subagent done]
← <final summary returned to parent>
```
Distinct prefix/indent so subagent work reads as "nested". Implementation: the
subagent's `agent_loop` renders through a `ux` mode that indents + dims.

### D7. Runaway guard — `maxTurns` cap ✅ (own choice, mirrors CC)

A subagent could loop forever (read → read → ...). Cap it:
`SUBAGENT_MAX_TURNS = 15`. When hit, the subagent stops and returns whatever it
has, with a note. Prevents a delegated task from burning unbounded tokens.

### D8. Subagent model — a cheaper model for read-only exploration (own choice, planned)

Today a subagent runs on the **same model** as the parent (`agent_loop` →
`llm_response` reads the global `MODEL`). CC runs its Explore subagents on
**Haiku** — read-only exploration doesn't need the flagship, and Haiku is ~5×
cheaper ($1/$5 per M vs Sonnet's $3/$15).

minicc's `task` is a **separate `agent_loop`**, which makes this a clean fit:

- Switching the **main** loop's model mid-session would invalidate its prompt
  cache (the cached prefix is model-scoped). A subagent is a **separate request
  stream**, so running it on Haiku **doesn't touch the parent's cached prefix** —
  exactly why CC spawns a subagent rather than swapping the main model.
- Two independent savings then stack: **(D5) context isolation** — the
  exploration never bloats parent context — **+ (D8) cheaper model** — the
  exploration itself costs ~5× less.

**Decision:** the subagent defaults to a cheaper model (Haiku); the parent stays
on the configured model.

**Implementation note:** `MODEL` is a module global and `set_model()` mutates it —
using that for the subagent would change the **parent's** model too. Thread a
per-call `model` override through `agent_loop` → `llm_response` → the API params
(don't mutate the global), alongside the `tools`/`max_turns` params the subagent
already passes.

**Status: designed, not implemented.**

## Interactions

### With context management (L1–L6) — synergistic
- The subagent has its **own** `messages`, so L3/L4 eviction/compaction apply
  **within** it independently. A long exploration inside a subagent can compact
  itself without touching the parent.
- The parent gains only the small final result → parent context stays lean. This
  is the cleanest context-management win available: don't put it in the main
  context at all.

### With usage / cost — counts toward the same totals
The subagent makes real API calls → tracked in the same `_USAGE`. `/cost`
reflects subagent spend (it's all the same bill). Once D8 lands (subagent on
Haiku), that spend drops ~5×, still counted in the same totals.

### With streaming
Subagent internal turns need **not** stream to the terminal (they're dimmed
progress, not the user's answer). Only the parent's final reply streams. Keeps
the display calm.

## What we defer (v0.3)
- **Background/async subagents** — sync only for v1.
- **Write-capable subagents** + permission inheritance — read-only avoids it.
- **Parallel subagents / agent teams** — out of scope.
- **User-invoked `/agent`** — model-invoked tool is enough for v1.

## Risks
- **Over-delegation**: the model might spawn subagents for trivial tasks (latency
  + cost). Mitigate with a clear tool description ("use only for exploration that
  would read many files") and watch during dogfood.
- **Lost detail**: parent only gets the summary; if it later needs a specific
  fact the subagent saw, it must re-derive (re-read, or re-delegate). Same
  detail-vs-space tradeoff as compaction. Acceptable.
- **Result size**: the subagent's final text goes into parent context — keep the
  subagent prompt biased toward a concise result.

## Implementation sketch

```python
# tools/task.py
SCHEMA = {
    "name": "task",
    "description": (
        "Delegate an exploration-heavy subtask to a subagent with its own "
        "context. Use ONLY when answering would require reading many files — "
        "the subagent explores in isolation and returns a concise summary, "
        "keeping this conversation's context clean. Read-only (no edits/bash)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"description": {"type": "string"}},
        "required": ["description"],
    },
}

def task(description: str) -> str:
    from minicc.agent import agent_loop          # local import avoids a cycle
    sub = [{"role": "user", "content": description}]
    agent_loop(sub, system=SUBAGENT_PROMPT, tools=READ_ONLY_TOOLS,
               max_turns=SUBAGENT_MAX_TURNS, ui="subagent")
    return _final_text(sub)
```

This requires `agent_loop` to accept `tools`, `max_turns`, and a `ui` mode —
small additions to the signature (the `system` param already exists from L1).

## Status
implemented
```
