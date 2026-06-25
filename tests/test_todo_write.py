"""Unit tests for the todo_write planning tool (stateless renderer)."""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from minicc.tools import todo_write as tw
from minicc.tools import TOOLS, TOOL_HANDLERS


def test_renders_status_marks():
    out = tw.todo_write(
        [
            {"content": "parse", "status": "completed"},
            {"content": "wire cli", "status": "in_progress"},
            {"content": "tests", "status": "pending"},
        ]
    )
    assert "✓ parse" in out
    assert "▶ wire cli" in out
    assert "☐ tests" in out


def test_empty_list():
    assert tw.todo_write([]) == "todos: (empty)"


def test_registered_in_tool_set():
    assert "todo_write" in TOOL_HANDLERS
    assert any(t["name"] == "todo_write" for t in TOOLS)
