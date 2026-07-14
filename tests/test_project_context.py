"""Tests for CLAUDE.md loading — CC's ancestor-directory walk.

The pinned behavior: a CLAUDE.md in ANY ancestor directory is loaded
automatically (CC's monorepo feature — observed live when a stray
~/Documents/CLAUDE.md was silently injected into every project under it),
outermost first, nearest last.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from minicc.prompts.system import load_project_context


def test_cwd_claude_md_loads(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("# Local rules")
    monkeypatch.chdir(tmp_path)
    out = load_project_context()
    assert "# Local rules" in out
    assert "— from" not in out  # cwd's own file carries no path label


def test_ancestor_claude_md_loads_outermost_first(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("ROOT CONVENTIONS")
    sub = tmp_path / "services" / "api"
    sub.mkdir(parents=True)
    (sub / "CLAUDE.md").write_text("SUBPROJECT RULES")
    monkeypatch.chdir(sub)
    out = load_project_context()
    # both present; outermost first so the nearest file reads last (wins)
    assert out.index("ROOT CONVENTIONS") < out.index("SUBPROJECT RULES")
    assert f"— from {tmp_path}" in out  # ancestor files are labeled with their path


def test_no_claude_md_anywhere(tmp_path, monkeypatch):
    sub = tmp_path / "deep"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert load_project_context() == ""


def test_per_file_truncation_still_applies(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("\n".join(f"line{i}" for i in range(300)))
    monkeypatch.chdir(tmp_path)
    out = load_project_context()
    assert "line199" in out and "line250" not in out
    assert "truncated at 200 lines" in out
