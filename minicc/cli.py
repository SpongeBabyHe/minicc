import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path
from minicc.agent import agent_loop


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return out or "untracked"
    except Exception:
        return "no-git"


def _print_header():
    print("=" * 64)
    print(f"SESSION  {datetime.now().isoformat(timespec='seconds')}")
    print(f"commit   {_git_sha()}")
    print(f"model    {os.environ.get('MODEL_ID', '?')}")
    print(f"cwd      {Path.cwd()}")
    print(f"os       {platform.system()}")
    print("=" * 64)


def main():
    _print_header()
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
        print(f"\n{'-' * 64}")
        print(f">>> USER (turn {turn})")
        print(query)

        history.append({"role": "user", "content": query})
        agent_loop(history)

        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            print(f"\n<<< ASSISTANT")
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)


if __name__ == "__main__":
    main()
