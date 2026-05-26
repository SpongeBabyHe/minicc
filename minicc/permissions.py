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


GATED_TOOLS = ["bash", "write_file", "edit_file"]
_ALLOWED = set()
MAX_PREVIEW = 500   # 单字段最多展示多少字符


def _truncate(s: str, n: int = MAX_PREVIEW) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n... [+{len(s) - n} more chars]"


def _format_args(tool_name: str, tool_input: dict) -> str:
    if tool_name == "bash":
        cmd = tool_input.get("command", "")
        return f"  cwd: {Path.cwd()}\n  cmd: {cmd}"

    if tool_name == "write_file":
        path = tool_input.get("path", "")
        content = tool_input.get("content", "")
        return (
            f"  path: {path}\n"
            f"  size: {len(content)} bytes\n"
            f"  preview:\n{_truncate(content)}"
        )

    if tool_name == "edit_file":
        path = tool_input.get("path", "")
        old = tool_input.get("old_text", "")
        new = tool_input.get("new_text", "")
        return (
            f"  path: {path}\n"
            f"  - old ({len(old)} chars):\n{_truncate(old, 300)}\n"
            f"  + new ({len(new)} chars):\n{_truncate(new, 300)}"
        )

    # fallback：没专门处理的工具直接打印
    return f"  {tool_input}"


def confirm(tool_name: str, tool_input: dict) -> bool:
    if tool_name not in GATED_TOOLS:
        return True
    if tool_name in _ALLOWED:
        return True
    print(_format_args(tool_name, tool_input))
    answer = input("Approve? [yes/no/all]: ").strip().lower()
    if answer == "all":
        _ALLOWED.add(tool_name)
        return True
    return answer == "yes"
