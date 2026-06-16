# Streaming output (Tier 1)

Render the model's reply token-by-token as it arrives, instead of waiting for
the whole response and dumping it at once. The single biggest perceived-quality
win for an interactive agent — and the most demo-able (a frozen terminal vs
text flowing in).

## Background

Today `llm_response` calls `client.messages.create(...)` and **blocks** until
the full reply is ready. The user sees a spinner, then the entire answer appears
at once. For a multi-second reply this feels dead.

Streaming uses `client.messages.stream(...)`: text arrives incrementally, we
print it as it comes. The user watches the agent "think out loud".

## Core mechanism (verified, SDK 0.100.0)

```python
with client.messages.stream(model=..., system=..., tools=..., messages=...) as stream:
    for delta in stream.text_stream:        # text deltas, as they arrive
        render(delta)
    final = stream.get_final_message()       # full Message: content blocks, usage, stop_reason
```

Verified facts:
- `stream()` takes the **same params** as `create()` (system blocks with
  `cache_control`, `tools`, `messages`).
- `stream.text_stream` yields only the **text** deltas.
- `stream.get_final_message()` returns the assembled `Message` — identical shape
  to what `create()` returns today (content blocks incl. `tool_use`, `.usage`,
  `.stop_reason`).

**The key consequence:** streaming changes *only how we obtain the assistant
text*. Everything downstream in the agent loop — tool dispatch, usage tracking,
stop-reason handling — stays the same, fed by `get_final_message()`.

## Decisions

### D1. Where streaming + rendering live — internal streaming ✅ (own choice)

Today: `agent_loop` does `with ux.thinking(): response = llm_response(messages)`.
Someone must now iterate `text_stream` and render. Options:

| Option | What | Trade-off |
|---|---|---|
| **A. llm_response streams internally** | iterate `text_stream`, print via `ux`, return final Message | simplest; llm.py already imports `ux` |
| B. `on_text` callback | `llm_response(messages, on_text=cb)`; caller renders | one more param + indirection; justified only by a future GUI |
| C. generator of events | `for event in agent_loop(...)` | full event-stream; big refactor |

**Choice: A (internal streaming).** `llm_response` iterates `text_stream` and
prints deltas via a small `ux` helper, then returns `get_final_message()`.

> **Revised from an earlier draft.** I first picked B (`on_text` callback)
> "as a step toward a future GUI event-stream". That's YAGNI — the GUI (Path A
> vs B) isn't even committed, so designing an abstraction for it now is
> speculation-driven, exactly the over-engineering we avoided in M4/M5. For a
> terminal-only tool, internal streaming is simpler and sufficient. If/when a
> GUI backend is real, introduce the callback/generator **then**, driven by that
> real requirement (which will also dictate its actual signature). llm.py
> already depends on `ux`, so "internal rendering couples layers" is not a new
> cost.

### D2. Rendering style — plain stream first, markdown deferred (own choice)

| Option | Look | Cost |
|---|---|---|
| **A. plain text, token-by-token** | no live markdown | trivial, responsive |
| B. rich `Live` + `Markdown`, re-render per delta | live formatted | flicker; partial markdown (half-open ```` ``` ````) renders wrong mid-stream; CPU |

**Chose A first, then B after one dogfood turn.** Plan was plain text (A) for
simplicity. First live test showed raw `**bold**` / `1.` markers everywhere —
too ugly for structured answers. Upgraded immediately to **B (live markdown)**:
a rich `Live` region re-renders `Markdown(accumulated)` as text grows (throttled
to ~10/s), then prints the final markdown permanently on exit. Cost: a Markdown
re-parse per refresh (bounded by refresh rate, not delta count) and a possible
faint flush at end-of-stream. Worth it — formatted output matters for the
portfolio demo.

> Dogfood overrode the design's conservative choice. Exactly what dogfood is
> for: a real run vetoed "plain text is fine".

### D3. Spinner handoff

`ux.thinking()` should show **until the first token**, then disappear and let
text stream. Flow: start spinner → on first `on_text` delta, stop spinner and
begin printing → continue streaming. The streaming helper owns this handoff so
callers don't manage spinner state.

### D4. Usage tracking

Read usage from `stream.get_final_message().usage` instead of `response.usage`.
Same fields (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`); `_USAGE` update logic is unchanged.

## Interactions

### With context management (L1–L6) — orthogonal, preserved
- `stream()` takes the same `system` blocks with `cache_control` and `tools`, so
  **L1 prompt caching still works**. Cache counters come from
  `get_final_message().usage` as before.
- L3 eviction / L4 compaction run **before** the call (inside `llm_response`,
  unchanged) → unaffected by streaming.
- `/cost`, `/context` keep working.

### With tool calls — unchanged
An assistant turn may contain text **and** `tool_use`. `text_stream` yields only
text; the `tool_use` blocks arrive in `get_final_message()`. So: stream the
text, get the final message, and if `stop_reason == "tool_use"` dispatch tools
exactly as today. The `→ tool(...)` / `← result` prints happen after, as now.
(The SDK also streams partial tool_use JSON via events; minicc does **not** need
to render that live — it reads the final input from `get_final_message()`.)

## What we defer
- **Live markdown during streaming** (D2) — start plain.
- **Streaming the tool_use input** as it's generated — not useful in a terminal.
- **Full event-generator refactor** (D1 option C) — the `on_text` callback is
  the stepping stone; the generator comes if/when we build the GUI backend.

## Risks
- **Markdown regression** (D2): streamed replies lose M6 formatting. Accepted
  for now; flagged for a polish pass.
- **Ctrl-C mid-stream**: the `with client.messages.stream(...)` context manager
  must close cleanly on `KeyboardInterrupt`; the existing cli.py interrupt
  handling should catch it and return to the prompt.
- **`tee` to a log**: streaming + rich control chars — same concern as M6;
  rich's non-tty detection should degrade to plain.

## Implementation sketch

```python
# llm.py
def llm_response(messages, system=None, stream=True):
    # ... L3/L4 budget management unchanged ...
    if not stream:
        response = client.messages.create(...same params...)   # tests, scripts
    else:
        with client.messages.stream(...same params...) as s:
            with ux.streaming() as render:   # spinner until first delta, then text
                for delta in s.text_stream:
                    render(delta)
            response = s.get_final_message()
    _USAGE[...] += response.usage...      # unchanged
    return response
```

```python
# ux.py — new
@contextmanager
def streaming():
    """Spinner until the first delta, then print deltas as plain text."""
    # yields a render(delta) callable; stops the spinner on first call
    ...
```

`agent.py` keeps calling `llm_response(messages)`; the tool-dispatch block below
is untouched. Tests pass `stream=False` for deterministic non-streaming.

## Status
⬜ Planned — design only. Implement after v0.2 ship.
```
