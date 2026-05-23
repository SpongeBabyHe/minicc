from . import bash

_MODULES = [bash]

TOOLS = [m.SCHEMA for m in _MODULES]
TOOL_HANDLERS = {m.SCHEMA["name"]: getattr(
    m, m.SCHEMA["name"]) for m in _MODULES}
