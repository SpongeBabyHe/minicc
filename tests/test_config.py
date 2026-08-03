"""Unit tests for config: model precedence, allowlist union, persistence.

Each test gets an isolated HOME (global settings) and cwd (project settings) via
monkeypatch, so nothing touches the real ~/.minicc.
"""

import os
import subprocess

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import json

import pytest

from minicc import config


@pytest.fixture(autouse=True)
def _fresh_settings_view():
    config.reset_active_settings()
    yield
    config.reset_active_settings()


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


def test_local_overrides_project_and_user_model(monkeypatch, tmp_path):
    home, proj = _setup(monkeypatch, tmp_path)
    _write(home / ".minicc" / "settings.json", {"default_model": "user"})
    _write(proj / ".minicc" / "settings.json", {"default_model": "project"})
    _write(proj / ".minicc" / "settings.local.json", {"default_model": "local"})
    assert config.resolve_model() == "local"


def test_discovery_preserves_source_order_and_metadata(monkeypatch, tmp_path):
    home, proj = _setup(monkeypatch, tmp_path)
    user_path = home / ".minicc" / "settings.json"
    project_path = proj / ".minicc" / "settings.json"
    local_path = proj / ".minicc" / "settings.local.json"
    _write(user_path, {"allowed_tools": ["edit_file"]})
    _write(project_path, {"allowed_tools": ["write_file", "edit_file"]})
    _write(local_path, {"allowed_tools": ["memory"]})

    snapshot = config.discover_settings()

    assert [source.scope for source in snapshot.sources] == [
        config.SettingsScope.USER,
        config.SettingsScope.PROJECT,
        config.SettingsScope.LOCAL,
    ]
    assert [source.path for source in snapshot.sources] == [
        user_path,
        project_path,
        local_path,
    ]
    assert snapshot.array("allowed_tools") == ["edit_file", "write_file", "memory"]


def test_restricted_view_exposes_only_user_source(monkeypatch, tmp_path):
    home, proj = _setup(monkeypatch, tmp_path)
    _write(home / ".minicc" / "settings.json", {"default_model": "user"})
    _write(proj / ".minicc" / "settings.json", {"default_model": "project"})
    _write(proj / ".minicc" / "settings.local.json", {"default_model": "local"})

    view = config.discover_settings().view(trusted=False)
    assert [source.scope for source in view.sources] == [config.SettingsScope.USER]
    assert view.scalar("default_model") == "user"

    config.activate(view)
    assert config.resolve_model() == "user"


def test_runtime_configuration_reads_the_active_view(monkeypatch, tmp_path):
    from minicc import llm
    from minicc import tools as tool_registry

    home, proj = _setup(monkeypatch, tmp_path)
    _write(
        home / ".minicc" / "settings.json",
        {"default_model": "user", "cache_ttl": "1h", "web_search": False},
    )
    _write(
        proj / ".minicc" / "settings.json",
        {"default_model": "project", "cache_ttl": "5m", "web_search": True},
    )
    config.activate(config.discover_settings().view(trusted=False))
    previous_model = llm.MODEL
    previous_ttl = llm.CACHE_TTL
    previous_tools = list(tool_registry.TOOLS)
    try:
        llm.configure_from_settings()
        tool_registry.configure_from_settings()
        assert llm.MODEL == "user"
        assert llm.CACHE_TTL == "1h"
        assert "web_search" not in {tool["name"] for tool in tool_registry.TOOLS}
    finally:
        llm.MODEL = previous_model
        llm.CACHE_TTL = previous_ttl
        tool_registry.TOOLS[:] = previous_tools


def test_malformed_settings_names_the_source(monkeypatch, tmp_path):
    _, proj = _setup(monkeypatch, tmp_path)
    (proj / ".minicc").mkdir(parents=True)
    path = proj / ".minicc" / "settings.json"
    path.write_text("{ not json")
    with pytest.raises(config.SettingsError, match=str(path)):
        config.resolve_model()


def test_project_scope_self_ignores(monkeypatch, tmp_path):
    _, proj = _setup(monkeypatch, tmp_path)
    config.set_default_model("claude-opus-4-8", scope="project")
    assert (proj / ".minicc" / ".gitignore").read_text() == (
        "*\n!.gitignore\n!settings.json\n"
    )


def test_custom_project_gitignore_is_not_replaced(monkeypatch, tmp_path):
    _, proj = _setup(monkeypatch, tmp_path)
    minicc_dir = proj / ".minicc"
    minicc_dir.mkdir()
    (minicc_dir / ".gitignore").write_text("sessions/\n")
    config.set_default_model("claude-opus-4-8", scope="project")
    assert (minicc_dir / ".gitignore").read_text() == "sessions/\n"


def test_gitignore_tracks_shared_settings_only(monkeypatch, tmp_path):
    _, proj = _setup(monkeypatch, tmp_path)
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    config.ensure_project_dir("sessions")
    (proj / ".minicc" / "settings.json").write_text("{}\n")
    (proj / ".minicc" / "settings.local.json").write_text("{}\n")
    (proj / ".minicc" / "sessions" / "one.jsonl").write_text("{}\n")

    def ignored(path):
        return subprocess.run(
            ["git", "-C", str(proj), "check-ignore", "-q", str(path)],
            check=False,
        ).returncode == 0

    assert not ignored(".minicc/settings.json")
    assert ignored(".minicc/settings.local.json")
    assert ignored(".minicc/sessions/one.jsonl")


def test_local_settings_live_at_nearest_repository_root(monkeypatch, tmp_path):
    home, proj = _setup(monkeypatch, tmp_path)
    (proj / ".git").mkdir()
    subdir = proj / "packages" / "app"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    path = config.set_default_model("local-model", scope="local")

    assert path == proj / ".minicc" / "settings.local.json"
    assert json.loads(path.read_text())["default_model"] == "local-model"
    assert home != proj


def test_explicit_start_dir_controls_project_and_local_discovery(monkeypatch, tmp_path):
    home, current = _setup(monkeypatch, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _write(current / ".minicc" / "settings.json", {"default_model": "wrong"})
    _write(other / ".minicc" / "settings.json", {"default_model": "right"})
    _write(home / ".minicc" / "settings.json", {"default_model": "user"})

    snapshot = config.discover_settings(other)

    assert snapshot.start_dir == other.resolve()
    assert snapshot.scalar("default_model") == "right"
    assert snapshot.source(config.SettingsScope.PROJECT).path == (
        other / ".minicc" / "settings.json"
    )


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


def test_resolve_cache_ttl_default_override_and_validation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert config.resolve_cache_ttl() == "5m"                    # default
    (tmp_path / "home" / ".minicc").mkdir(parents=True)
    (tmp_path / "home" / ".minicc" / "settings.json").write_text('{"cache_ttl": "1h"}')
    assert config.resolve_cache_ttl() == "1h"                    # global setting
    (tmp_path / "proj" / ".minicc").mkdir(parents=True)
    (tmp_path / "proj" / ".minicc" / "settings.json").write_text('{"cache_ttl": "5m"}')
    assert config.resolve_cache_ttl() == "5m"                    # project overrides
    (tmp_path / "proj" / ".minicc" / "settings.json").write_text('{"cache_ttl": "2h"}')
    assert config.resolve_cache_ttl() == "5m"                    # invalid → fallback
