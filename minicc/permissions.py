# Trust model for minicc:
#   - Reads (read_file, glob, grep) are not gated. The model can see anything
#     this process can see. Don't run minicc on machines with other people's data.
#   - Writes (write_file, edit_file) are gated regardless of whether they
#     create or overwrite — the tool can't tell the difference in advance.
#   - bash is gated because we don't know what it will do. NOTE: 'a'-approving
#     bash effectively disables all gating for network calls, package installs,
#     git push, kill, etc. Use 'y' for bash; reserve 'a' for write_file/edit_file.
#   - Scope escapes (paths outside cwd) are NOT detected. Add later if needed.
#
# Full trust model + permission-layer vs execution-layer analysis: PERMISSIONS.md


from pathlib import Path
from minicc import ux

# Gated tools: these require user approval before use.
GATED_TOOLS = ["bash", "write_file", "edit_file"]
# session-scoped allowed tools if user answers "all" to the prompt
_ALLOWED = set()

# Gated tools that may NOT be pre-approved from config (preload). bash's effect is
# unbounded + irreversible and the gate is its ONLY boundary, so trusting it must
# stay a per-session decision, never a persistent settings entry. See PERMISSIONS.md.
NO_PRELOAD = {"bash"}


def _format_args(tool_name: str, tool_input: dict) -> str:
    if tool_name == "bash":
        return ux.kv_block(
            [
                ("cwd", Path.cwd()),
                ("cmd", tool_input.get("command", "")),
            ]
        )
    if tool_name == "write_file":
        content = tool_input.get("content", "")
        return ux.kv_block(
            [
                ("path", tool_input.get("path", "")),
                ("size", f"{len(content)} bytes"),
                ("preview", ux.truncate(content, 500)),
            ]
        )
    if tool_name == "edit_file":
        return ux.diff_view(
            tool_input.get("old_text", ""),
            tool_input.get("new_text", ""),
            tool_input.get("path", ""),
        )
    return ux.kv_block(list(tool_input.items()))


def confirm(tool_name: str, tool_input: dict) -> bool:
    if tool_name not in GATED_TOOLS:
        return True
    if tool_name in _ALLOWED:
        return True
    ux.say(_format_args(tool_name, tool_input))
    answer = input("Approve? [yes/no/all]: ").strip().lower()
    if answer == "all":
        _ALLOWED.add(tool_name)
        return True
    return answer == "yes"


def reset():
    """Clear the session-scoped allowed-tools set. Called by /clear."""
    _ALLOWED.clear()


def preload(tools) -> set:
    """Pre-approve gated tools from config at startup (re-applied after /clear).
    Excludes NO_PRELOAD (bash) and non-gated names. Returns the set applied, so
    the caller can surface which tools now skip the prompt."""
    applied = {t for t in tools if t in GATED_TOOLS and t not in NO_PRELOAD}
    _ALLOWED.update(applied)
    return applied
