"""Tests for the file-freshness contract (CC parity: read-before-edit +
staleness detection + post-edit snippet).

The contract this pins, in CC's own observed wording: a file must be read
before it may be edited; an edit is rejected if the file changed on disk since
that read; the agent's own writes keep the file fresh (consecutive edits need
no re-read); external changes (user/linter/bash) force a re-read.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc.tools import freshness
from minicc.tools.edit_file import edit_file
from minicc.tools.read_file import read_file
from minicc.tools.write_file import write_file


@pytest.fixture(autouse=True)
def _clean_registry():
    freshness.reset()
    yield
    freshness.reset()


def test_edit_without_read_is_rejected(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    out = edit_file(str(f), "x = 1", "x = 2")
    assert out.startswith("Error: File has not been read yet")
    assert f.read_text() == "x = 1\n"  # untouched


def test_read_then_edit_succeeds_with_snippet(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("a = 1\nb = 2\nc = 3\n")
    read_file(str(f))
    out = edit_file(str(f), "b = 2", "b = 20")
    assert out.startswith(f"Edited {f} (lines 2-2):")
    assert "b = 20" in out and "a = 1" in out  # edited line + context, numbered
    assert "\t" in out


def test_own_edit_keeps_file_fresh(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("a = 1\nb = 2\n")
    read_file(str(f))
    assert edit_file(str(f), "a = 1", "a = 10").startswith("Edited")
    # consecutive edit WITHOUT re-reading must work (our write re-recorded)
    assert edit_file(str(f), "b = 2", "b = 20").startswith("Edited")


def test_external_change_forces_reread(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("a = 1\n")
    read_file(str(f))
    f.write_text("a = 999\n")            # user/linter/bash writes behind our back
    os.utime(f, (1, 1))                  # force a distinct mtime deterministically
    out = edit_file(str(f), "a = 999", "a = 2")
    assert out.startswith("Error: File has been modified since it was last read")
    # re-reading clears the staleness
    read_file(str(f))
    assert edit_file(str(f), "a = 999", "a = 2").startswith("Edited")


def test_write_file_new_file_is_free_and_records(tmp_path):
    f = tmp_path / "new.txt"
    assert write_file(str(f), "hello").startswith("Wrote")
    # our own write recorded freshness → an immediate edit is allowed
    assert edit_file(str(f), "hello", "world").startswith("Edited")


def test_write_file_overwrite_unread_is_rejected(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("original")
    out = write_file(str(f), "clobbered")
    assert out.startswith("Error: File has not been read yet")
    assert f.read_text() == "original"
    read_file(str(f))
    assert write_file(str(f), "replaced").startswith("Wrote")


def test_reset_forgets_reads(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    read_file(str(f))
    freshness.reset()                    # /clear
    out = edit_file(str(f), "x = 1", "x = 2")
    assert out.startswith("Error: File has not been read yet")


def test_snippet_elides_huge_insertions(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("start\nEND\n")
    read_file(str(f))
    big = "\n".join(f"line{i}" for i in range(40))
    out = edit_file(str(f), "END", big)
    assert out.startswith("Edited")
    assert "..." in out                  # elision marker
    assert out.count("\n") < 20          # bounded, not the whole 40-line insert
