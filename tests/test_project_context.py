"""Tests for CLAUDE.md loading — CC's ancestor-directory walk.

The pinned behavior: a CLAUDE.md in ANY ancestor directory is loaded
automatically (CC's monorepo feature — observed live when a stray
~/Documents/CLAUDE.md was silently injected into every project under it),
outermost first, nearest last. Delivery — the claudeMd system-reminder with
per-file "Contents of <path> …" labels — is covered in test_reminders.py;
this file pins the walk itself.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import config
from minicc.reminders import claude_md_files


@pytest.fixture(autouse=True)
def _fresh_settings_view():
    config.reset_active_settings()
    yield
    config.reset_active_settings()


def _trust_workspace():
    config.activate(config.discover_settings().view(trusted=True))


def test_untrusted_workspace_does_not_load_claude_md(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("untrusted instructions")
    monkeypatch.chdir(tmp_path)

    assert claude_md_files() == []


def test_cwd_claude_md_loads(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("# Local rules")
    monkeypatch.chdir(tmp_path)
    _trust_workspace()
    files = claude_md_files()
    assert files == [(tmp_path / "CLAUDE.md", "# Local rules")]


def test_ancestor_claude_md_loads_outermost_first(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("ROOT CONVENTIONS")
    sub = tmp_path / "services" / "api"
    sub.mkdir(parents=True)
    (sub / "CLAUDE.md").write_text("SUBPROJECT RULES")
    monkeypatch.chdir(sub)
    _trust_workspace()
    files = claude_md_files()
    # both present; outermost first so the nearest file reads last (wins)
    assert [t for _p, t in files] == ["ROOT CONVENTIONS", "SUBPROJECT RULES"]
    assert files[0][0] == tmp_path / "CLAUDE.md"  # each carries its real path


def test_no_claude_md_anywhere(tmp_path, monkeypatch):
    sub = tmp_path / "deep"
    sub.mkdir()
    monkeypatch.chdir(sub)
    _trust_workspace()
    assert claude_md_files() == []


def test_claude_md_loads_in_full(tmp_path, monkeypatch):
    """No truncation — CC's memory doc: "CLAUDE.md files are loaded in full
    regardless of length"; the 200-line/25KB cap is MEMORY.md-only."""
    (tmp_path / "CLAUDE.md").write_text("\n".join(f"line{i}" for i in range(300)))
    monkeypatch.chdir(tmp_path)
    _trust_workspace()
    (_path, text), = claude_md_files()
    assert "line299" in text
    assert "truncated" not in text
