import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from minicc.agent import agent_loop
from minicc import ux


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return out or "untracked"
    except Exception:
        return "no-git"


def _session_info() -> dict:
    """Pure data about this session — no presentation."""
    return {
        "SESSION": datetime.now().isoformat(timespec="seconds"),
        "commit":  _git_sha(),
        "model":   os.environ.get("MODEL_ID", "?"),
        "cwd":     str(Path.cwd()),
        "os":      platform.system(),
    }


def main():
    ux.console.rule()
    ux.say(ux.kv_block(list(_session_info().items()), indent=""), style=ux.S_INFO)
    ux.console.rule()
    history = []
    turn = 0
    while True:
        try:
            query = input("\nQuery: ")
        except (EOFError, KeyboardInterrupt):
            break
        if not query.strip():
            continue
        if query.strip().lower() in ("q", "exit", "quit"):
            break

        turn += 1
        ux.say(f">>> USER (turn {turn})", style=ux.S_USER)

        history.append({"role": "user", "content": query})

        try:
            agent_loop(history)
        except KeyboardInterrupt:
            ux.say("interrupted", style=ux.S_INFO)
            continue
        except Exception as e:
            ux.say(f"agent error: {e!r}", style=ux.S_ERROR)
            continue

        last_content = history[-1]["content"]
        if isinstance(last_content, list):
            text = "\n".join(
                b.text for b in last_content if hasattr(b, "text"))
            if text:
                ux.say("<<< ASSISTANT", style=ux.S_ASSISTANT)
                ux.markdown(text)


if __name__ == "__main__":
    main()
