from . import bash, read_file, write_file, edit_file, glob, grep

_MODULES = [bash, read_file, write_file, edit_file, glob, grep]

_TOOLS_RAW = [m.SCHEMA for m in _MODULES]
# cache_control on the LAST tool marks the whole tools block as cache prefix
# layer 3. Copy the dict (don't mutate the module's SCHEMA — TOOL_HANDLERS
# still reads the originals).
TOOLS = _TOOLS_RAW[:-1] + [{**_TOOLS_RAW[-1], "cache_control": {"type": "ephemeral"}}]
TOOL_HANDLERS = {m.SCHEMA["name"]: getattr(m, m.SCHEMA["name"]) for m in _MODULES}
