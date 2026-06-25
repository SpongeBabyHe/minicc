"""Unit tests for config: model precedence, allowlist union, persistence.

Each test gets an isolated HOME (global settings) and cwd (project settings) via
monkeypatch, so nothing touches the real ~/.minicc.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import json

from minicc import config


def _setup(monkeypatch, tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))   # Path.home() → temp; _global() reads it live
    monkeypatch.chdir(proj)                 # _project() → temp cwd
    return home, proj


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_default_when_no_settings(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert config.resolve_model() == config.DEFAULT_MODEL


def test_global_default_persisted_and_read(monkeypatch, tmp_path):
    home, _ = _setup(monkeypatch, tmp_path)
    path = config.set_default_model("claude-opus-4-8")          # default scope = global
    assert path == home / ".minicc" / "settings.json"
    assert config.resolve_model() == "claude-opus-4-8"


def test_project_overrides_global_model(monkeypatch, tmp_path):
    home, proj = _setup(monkeypatch, tmp_path)
    _write(home / ".minicc" / "settings.json", {"default_model": "claude-opus-4-8"})
    _write(proj / ".minicc" / "settings.json", {"default_model": "claude-haiku-4-5-20251001"})
    assert config.resolve_model() == "claude-haiku-4-5-20251001"


def test_malformed_settings_ignored(monkeypatch, tmp_path):
    _, proj = _setup(monkeypatch, tmp_path)
    (proj / ".minicc").mkdir(parents=True)
    (proj / ".minicc" / "settings.json").write_text("{ not json")
    assert config.resolve_model() == config.DEFAULT_MODEL   # falls back, no crash


def test_project_scope_self_ignores(monkeypatch, tmp_path):
    _, proj = _setup(monkeypatch, tmp_path)
    config.set_default_model("claude-opus-4-8", scope="project")
    assert (proj / ".minicc" / ".gitignore").read_text() == "*\n"


def test_allowed_tools_union(monkeypatch, tmp_path):
    home, proj = _setup(monkeypatch, tmp_path)
    _write(home / ".minicc" / "settings.json", {"allowed_tools": ["edit_file"]})
    _write(proj / ".minicc" / "settings.json", {"allowed_tools": ["write_file"]})
    assert config.allowed_tools() == ["edit_file", "write_file"]   # sorted union


def test_allowed_tools_empty_when_unset(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert config.allowed_tools() == []


def test_preload_excludes_bash_and_non_gated():
    from minicc import permissions
    permissions.reset()
    applied = permissions.preload(["write_file", "edit_file", "read_file", "bash", "bogus"])
    assert applied == {"write_file", "edit_file"}     # bash excluded; read_file/bogus not gated
    assert "bash" not in permissions._ALLOWED         # bash never pre-approved from config
    permissions.reset()
