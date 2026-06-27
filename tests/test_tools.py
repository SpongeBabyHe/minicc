"""Unit tests for the file/search tool handlers.

Focus on the behaviors the schema descriptions promise (so description and code
can't drift): edit_file uniqueness, grep's file:line: prefixes + accurate caps,
read_file's offset/limit window, write_file's byte count, and bash's safety
filter precision.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import shutil

import pytest

from minicc.tools import bash as bash_mod
from minicc.tools import edit_file as edit_mod
from minicc.tools import grep as grep_mod
from minicc.tools import read_file as read_mod
from minicc.tools import write_file as write_mod
from minicc.tools import TOOLS, TOOL_HANDLERS


# ─── edit_file: the description promises EXACTLY-ONCE; code must enforce it ──

def test_edit_replaces_unique_occurrence(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\n")
    out = edit_mod.edit_file(str(f), "y = 2", "y = 3")
    assert out == f"Edited {f}"
    assert f.read_text() == "x = 1\ny = 3\n"


def test_edit_rejects_multiple_occurrences(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v = 0\nv = 0\n")
    out = edit_mod.edit_file(str(f), "v = 0", "v = 1")
    assert out.startswith("Error:")
    assert "appears 2 times" in out
    assert f.read_text() == "v = 0\nv = 0\n"  # unchanged — no silent first-match edit


def test_edit_rejects_missing_text(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("hello\n")
    out = edit_mod.edit_file(str(f), "nope", "x")
    assert out.startswith("Error:")
    assert "not found" in out


# ─── grep: description promises `file:line:` prefixes and per-file cap ───────

@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_emits_file_line_prefixes(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    out = grep_mod.grep("def ", str(f))
    # file:line:content — the line number must be present (rg drops it under capture
    # unless --line-number is passed, which is exactly what we fixed).
    assert f"{f}:1:def foo():" in out
    assert f"{f}:4:def bar():" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_no_match(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("nothing here\n")
    assert grep_mod.grep("zzz", str(f)) == "No matches."


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_bad_regex_is_error_not_no_match(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("hello\n")
    out = grep_mod.grep("(unclosed", str(f))
    assert out.startswith("Error:")  # not a misleading "No matches."


# ─── read_file: description promises offset/limit windowing ─────────────────

def test_read_offset_and_limit(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    out = read_mod.read_file(str(f), offset=3, limit=2)
    assert "line3" in out and "line4" in out
    assert "line2" not in out and "line5" not in out
    assert "Showing lines 3-4 of 10" in out


def test_read_whole_file_no_notice(tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("one\ntwo\n")
    out = read_mod.read_file(str(f))
    assert out == "one\ntwo"  # no truncation notice when fully returned


# ─── write_file: description promises a byte count ──────────────────────────

def test_write_reports_bytes(tmp_path):
    f = tmp_path / "out.txt"
    out = write_mod.write_file(str(f), "héllo")  # 'é' is 2 bytes in UTF-8
    assert "6 bytes" in out  # 5 chars, 6 bytes
    assert f.read_text() == "héllo"


# ─── bash: safety filter catches catastrophes, not legitimate commands ──────

def _blocked(cmd):
    return any(p.search(cmd) for p in bash_mod._DANGEROUS)


def test_bash_blocks_catastrophic():
    assert _blocked("rm -rf /")
    assert _blocked("rm -rf /*")
    assert _blocked("sudo rm x")
    assert _blocked("shutdown now")
    assert _blocked("mkfs.ext4 /dev/sda1")


def test_bash_allows_legitimate_commands():
    # These were false-positived by the old substring filter.
    assert not _blocked("rm -rf /tmp/scratch")
    assert not _blocked("echo hi 2>/dev/null")
    assert not _blocked("echo pseudocode")
    assert not _blocked("cat /dev/null")


def test_bash_runs_simple_command():
    assert bash_mod.bash("echo pseudocode").strip() == "pseudocode"


# ─── registration sanity ────────────────────────────────────────────────────

def test_all_changed_tools_registered():
    for name in ("bash", "edit_file", "grep", "read_file", "write_file"):
        assert name in TOOL_HANDLERS
        assert any(t["name"] == name for t in TOOLS)
