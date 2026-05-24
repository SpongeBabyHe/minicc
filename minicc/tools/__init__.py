from . import bash, read_file, write_file, edit_file, glob, grep

_MODULES = [bash, read_file, write_file, edit_file, glob, grep]

TOOLS = [m.SCHEMA for m in _MODULES]
TOOL_HANDLERS = {
    m.SCHEMA["name"]: getattr(
        m, m.SCHEMA["name"]) for m in _MODULES
}
