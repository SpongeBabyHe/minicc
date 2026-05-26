# Eval infra: what we explicitly chose NOT to build

These were considered and deferred because we haven't felt the pain yet.
Revisit when the corresponding pain appears.

- **Token/cost tracking** → wait for M6
- **Per-step timestamps** → add if we hit a "why was this slow?" moment
- **JSON structured logs** → add if we ever want to grep logs programmatically
- **`rich` coloring / panels** → M6 polish
- **Dedicated `trace.py` module** → extract when agent.py + cli.py
  tracing helpers cross ~30 lines combined
- **Auto-running task suite** → currently we type tasks by hand;
  scripting this is tempting but loses the "human reads each transcript"
  loop that makes eval valuable