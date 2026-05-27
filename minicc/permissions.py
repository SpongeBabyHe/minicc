# Trust model for minicc:
#   - Reads (read_file, glob, grep) are not gated. The model can see anything
#     this process can see. Don't run minicc on machines with other people's data.
#   - Writes (write_file, edit_file) are gated regardless of whether they
#     create or overwrite — the tool can't tell the difference in advance.
#   - bash is gated because we don't know what it will do. NOTE: 'a'-approving
#     bash effectively disables all gating for network calls, package installs,
#     git push, kill, etc. Use 'y' for bash; reserve 'a' for write_file/edit_file.
#   - Scope escapes (paths outside cwd) are NOT detected. Add later if needed.


from pathlib import Path
from minicc import ux


GATED_TOOLS = ["bash", "write_file", "edit_file"]
_ALLOWED = set()


def _format_args(tool_name: str, tool_input: dict) -> str:
    if tool_name == "bash":
        return ux.kv_block([
            ("cwd", Path.cwd()),
            ("cmd", tool_input.get("command", "")),
        ])
    if tool_name == "write_file":
        content = tool_input.get("content", "")
        return ux.kv_block([
            ("path", tool_input.get("path", "")),
            ("size", f"{len(content)} bytes"),
            ("preview", ux.truncate(content, 500)),
        ])
    if tool_name == "edit_file":
        return ux.kv_block([
            ("path", tool_input.get("path", "")),
            ("- old", ux.truncate(tool_input.get("old_text", ""), 300)),
            ("+ new", ux.truncate(tool_input.get("new_text", ""), 300)),
        ])
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
