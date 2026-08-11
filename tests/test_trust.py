"""Workspace Trust persistence and CLI settings activation."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import cli, config, permissions
from minicc.trust import TrustError, TrustManager
from minicc.workspace import local_settings_are_repository_supplied


@pytest.fixture(autouse=True)
def _fresh_settings_view():
    config.reset_active_settings()
    yield
    config.reset_active_settings()


def _linked_worktree(tmp_path):
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    git_dir = main / ".git"
    administration = git_dir / "worktrees" / "topic"
    administration.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {administration}\n")
    (administration / "commondir").write_text("../..\n")
    (administration / "gitdir").write_text(str(worktree / ".git") + "\n")
    return main, worktree


def test_acceptance_persists_for_one_canonical_directory(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    store = home / ".minicc" / "trust.json"

    manager = TrustManager(store_path=store, home=home)
    assert manager.ensure_trusted(project, lambda path: path == project.resolve())

    assert json.loads(store.read_text()) == {
        "trusted_workspaces": [str(project.resolve())]
    }
    assert TrustManager(store_path=store, home=home).is_trusted(project)
    assert not TrustManager(store_path=store, home=home).is_trusted(tmp_path)


def test_existing_acceptance_skips_confirmation(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    store = home / ".minicc" / "trust.json"
    TrustManager(store_path=store, home=home).accept(project)

    manager = TrustManager(store_path=store, home=home)

    def unexpected_confirmation(_path):
        raise AssertionError("persisted Trust should not prompt again")

    assert manager.ensure_trusted(project, unexpected_confirmation)


def test_repository_subdirectories_share_one_trust_identity(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    subdir = project / "packages" / "app"
    home.mkdir()
    (project / ".git").mkdir(parents=True)
    subdir.mkdir(parents=True)
    store = home / ".minicc" / "trust.json"
    manager = TrustManager(store_path=store, home=home)

    assert manager.ensure_trusted(
        subdir,
        lambda identity: identity == project.resolve(),
    )

    assert json.loads(store.read_text()) == {
        "trusted_workspaces": [str(project.resolve())]
    }
    restored = TrustManager(store_path=store, home=home)
    assert restored.is_trusted(project)
    assert restored.is_trusted(subdir)


def test_linked_worktrees_share_main_checkout_trust_identity(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    main, worktree = _linked_worktree(tmp_path)
    subdir = worktree / "src"
    subdir.mkdir()
    store = home / ".minicc" / "trust.json"
    manager = TrustManager(store_path=store, home=home)

    manager.accept(subdir)

    assert manager.workspace_identity(subdir) == main.resolve()
    assert json.loads(store.read_text()) == {
        "trusted_workspaces": [str(main.resolve())]
    }
    assert TrustManager(store_path=store, home=home).is_trusted(worktree)


def test_trusted_parent_covers_descendant_without_exact_trust(tmp_path):
    home = tmp_path / "home"
    parent = tmp_path / "projects"
    child = parent / "new-project"
    home.mkdir()
    child.mkdir(parents=True)
    store = home / ".minicc" / "trust.json"
    TrustManager(store_path=store, home=home).accept(parent)

    restored = TrustManager(store_path=store, home=home)
    assert restored.is_trusted(child)
    assert not restored.is_explicitly_trusted(child)


def test_worktree_git_file_defines_repository_trust_identity(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "worktree"
    subdir = project / "src"
    home.mkdir()
    project.mkdir()
    (project / ".git").write_text("gitdir: /untrusted/path\n")
    subdir.mkdir()

    manager = TrustManager(store_path=tmp_path / "trust.json", home=home)

    assert manager.workspace_identity(subdir) == project.resolve()


def test_worktree_pointer_cannot_borrow_another_checkout_identity(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _main, legitimate_worktree = _linked_worktree(tmp_path)
    administration = Path(
        (legitimate_worktree / ".git").read_text().removeprefix("gitdir: ").strip()
    )
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / ".git").write_text(f"gitdir: {administration}\n")

    manager = TrustManager(store_path=tmp_path / "trust.json", home=home)

    assert manager.workspace_identity(attacker) == attacker.resolve()


def test_home_git_marker_does_not_widen_child_directory_trust(tmp_path):
    home = tmp_path / "home"
    child = home / "scratch"
    (home / ".git").mkdir(parents=True)
    child.mkdir()
    store = home / ".minicc" / "trust.json"
    manager = TrustManager(store_path=store, home=home)

    manager.accept(child)

    assert json.loads(store.read_text()) == {
        "trusted_workspaces": [str(child.resolve())]
    }
    assert not TrustManager(store_path=store, home=home).is_trusted(home)


def test_home_acceptance_is_session_only(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = home / ".minicc" / "trust.json"
    manager = TrustManager(store_path=store, home=home)

    manager.accept(home)

    assert manager.is_trusted(home)
    assert not store.exists()
    assert not TrustManager(store_path=store, home=home).is_trusted(home)


def test_decline_does_not_persist(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    store = home / ".minicc" / "trust.json"
    manager = TrustManager(store_path=store, home=home)

    assert not manager.ensure_trusted(project, lambda _path: False)
    assert not store.exists()


def test_malformed_store_is_reported(tmp_path):
    store = tmp_path / "trust.json"
    store.write_text("{broken")

    with pytest.raises(TrustError, match=str(store)):
        TrustManager(store_path=store, home=tmp_path).is_trusted(tmp_path / "project")


def test_non_string_trust_store_entry_is_reported(tmp_path):
    store = tmp_path / "trust.json"
    store.write_text(json.dumps({"trusted_workspaces": [["not", "a", "path"]]}))

    with pytest.raises(TrustError, match=str(store)):
        TrustManager(store_path=store, home=tmp_path).is_trusted(tmp_path / "project")


def _settings_environment(monkeypatch, tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        config, "_user_settings_path", lambda: home / ".minicc" / "settings.json"
    )
    monkeypatch.setattr(
        config,
        "_shared_project_settings_path",
        lambda: project / ".minicc" / "settings.json",
    )
    monkeypatch.setattr(
        config,
        "_local_project_settings_path",
        lambda: project / ".minicc" / "settings.local.json",
    )
    (home / ".minicc").mkdir()
    (project / ".minicc").mkdir()
    (home / ".minicc" / "settings.json").write_text(
        json.dumps({"default_model": "user-model"})
    )
    (project / ".minicc" / "settings.json").write_text(
        json.dumps({"default_model": "project-model"})
    )
    return home, project


def test_unbound_settings_are_user_only(tmp_path, monkeypatch):
    _home, _project = _settings_environment(monkeypatch, tmp_path)

    assert not config.current_settings().trusted
    assert config.resolve_model() == "user-model"


def test_untrusted_config_roots_expose_only_personal_files(tmp_path, monkeypatch):
    home, project = _settings_environment(monkeypatch, tmp_path)

    assert list(config.config_roots("skills")) == [home / ".minicc" / "skills"]

    config.activate(config.discover_settings().view(trusted=True))
    roots = list(config.config_roots("skills"))
    assert project / ".minicc" / "skills" in roots
    assert roots[-1] == home / ".minicc" / "skills"


def test_trusted_config_roots_stop_at_workspace_boundary(tmp_path, monkeypatch):
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    project = outside / "project"
    subdir = project / "packages" / "app"
    home.mkdir()
    (project / ".git").mkdir(parents=True)
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    config.activate(config.discover_settings().view(trusted=True))

    roots = list(config.config_roots("skills"))

    assert roots == [
        project / ".minicc" / "skills",
        project / "packages" / ".minicc" / "skills",
        subdir / ".minicc" / "skills",
        home / ".minicc" / "skills",
    ]
    assert outside / ".minicc" / "skills" not in roots


def test_cli_accepts_then_activates_project_settings(tmp_path, monkeypatch):
    home, project = _settings_environment(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")

    assert cli._activate_workspace_settings()

    assert config.current_settings().trusted
    assert config.resolve_model() == "project-model"
    stored = json.loads((home / ".minicc" / "trust.json").read_text())
    assert stored["trusted_workspaces"] == [str(project.resolve())]


def test_cli_decline_leaves_only_user_settings_visible(tmp_path, monkeypatch):
    home, _project = _settings_environment(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert not cli._activate_workspace_settings()

    assert not config.current_settings().trusted
    assert config.resolve_model() == "user-model"
    assert not (home / ".minicc" / "trust.json").exists()


def test_home_local_grants_apply_before_workspace_trust(tmp_path, monkeypatch):
    home = tmp_path / "home"
    settings_dir = home / ".minicc"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text("{}")
    (settings_dir / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(local *)"]}})
    )
    monkeypatch.chdir(home)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert not cli._activate_workspace_settings()
    permissions.reset()

    view = config.current_settings()
    assert not view.trusted
    assert view.local_grants_trusted
    assert not permissions._is_gated("bash", {"command": "local test"})


def test_cli_trust_prompt_summarizes_gated_permissions(tmp_path, monkeypatch):
    home, project = _settings_environment(monkeypatch, tmp_path)
    (home / ".minicc" / "settings.json").write_text(
        json.dumps(
            {
                "default_model": "user-model",
                "permissions": {"allow": ["Bash(user *)"]},
            }
        )
    )
    (project / ".minicc" / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": [
                        "Bash(uv run *)",
                        "Bash(fake)\nTrust this workspace? yes",
                        "Bash(\u001b[31mred\u001b[0m *)",
                    ],
                    "ask": ["Read(/review/**)"],
                    "deny": ["Bash(git push *)"],
                    "additionalDirectories": ["../shared"],
                },
                "allowed_tools": ["write_file", "bash", "read_file", "bogus"],
            }
        )
    )
    shown = []
    monkeypatch.setattr(cli.ux.console, "rule", lambda: None)
    monkeypatch.setattr(
        cli.ux,
        "say",
        lambda message, **_kwargs: shown.append(str(message)),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert not cli._activate_workspace_settings()

    preview = "\n".join(shown)
    assert "This folder pre-approves 3 tool permissions" in preview
    assert ".minicc/settings.json" in preview
    assert "Bash(uv run *)" in preview
    assert "Bash(red *)" in preview
    assert "[31m" not in preview
    assert "Bash(fake)Trust this workspace? yes" not in preview
    assert "Bash(fake)\nTrust this workspace? yes" not in preview
    assert "This folder adds 1 directory to the workspace" in preview
    assert "../shared" in preview
    assert "write_file" in preview
    assert "These tool permissions will apply without asking" in preview
    assert "does not yet enforce Claude Code's filesystem boundary" in preview
    assert "bogus" not in preview
    assert "read_file" not in preview
    assert "Bash(user *)" not in preview
    assert "Read(/review/**)" not in preview
    assert "Bash(git push *)" not in preview
    assert "Project-requested grants" not in preview


def test_parent_trust_activates_project_config_but_not_nested_grants(
    tmp_path,
    monkeypatch,
):
    home, project = _settings_environment(monkeypatch, tmp_path)
    (project / ".minicc" / "settings.json").write_text(
        json.dumps(
            {
                "default_model": "project-model",
                "permissions": {"allow": ["Bash(npm run *)"]},
            }
        )
    )
    TrustManager(
        store_path=home / ".minicc" / "trust.json",
        home=home,
    ).accept(tmp_path)
    prompts = []
    monkeypatch.setattr(cli.ux.console, "rule", lambda: None)
    monkeypatch.setattr(cli.ux, "say", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "no",
    )

    assert cli._activate_workspace_settings()

    view = config.current_settings()
    assert view.trusted
    assert not view.project_grants_trusted
    assert view.local_grants_trusted
    assert config.resolve_model() == "project-model"
    assert prompts and "continues without these permissions" in prompts[0]
    assert not TrustManager(
        store_path=home / ".minicc" / "trust.json",
        home=home,
    ).is_explicitly_trusted(project)


def test_parent_trust_skips_backstop_when_nested_project_has_no_grants(
    tmp_path,
    monkeypatch,
):
    home, _project = _settings_environment(monkeypatch, tmp_path)
    TrustManager(
        store_path=home / ".minicc" / "trust.json",
        home=home,
    ).accept(tmp_path)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(
            AssertionError("a covered workspace without grants must not prompt")
        ),
    )

    assert cli._activate_workspace_settings()
    assert config.current_settings().trusted
    assert not config.current_settings().project_grants_trusted
    assert config.current_settings().local_grants_trusted
    assert config.resolve_model() == "project-model"


def test_parent_trust_applies_untracked_local_grants_without_backstop(
    tmp_path,
    monkeypatch,
):
    home, project = _settings_environment(monkeypatch, tmp_path)
    (project / ".minicc" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(local *)"]}})
    )
    TrustManager(
        store_path=home / ".minicc" / "trust.json",
        home=home,
    ).accept(tmp_path)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(
            AssertionError("an untracked local grant must not trigger backstop")
        ),
    )

    assert cli._activate_workspace_settings()
    permissions.reset()

    view = config.current_settings()
    assert view.local_grants_trusted
    assert not permissions._is_gated("bash", {"command": "local test"})


def test_parent_trust_keeps_tracked_local_grants_behind_backstop(
    tmp_path,
    monkeypatch,
):
    home, project = _settings_environment(monkeypatch, tmp_path)
    local_path = project / ".minicc" / "settings.local.json"
    local_path.write_text(
        json.dumps({"permissions": {"allow": ["Bash(tracked *)"]}})
    )
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "add", ".minicc/settings.local.json"],
        cwd=project,
        check=True,
    )
    TrustManager(
        store_path=home / ".minicc" / "trust.json",
        home=home,
    ).accept(tmp_path)
    shown = []
    monkeypatch.setattr(cli.ux.console, "rule", lambda: None)
    monkeypatch.setattr(
        cli.ux,
        "say",
        lambda message, **_kwargs: shown.append(str(message)),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert cli._activate_workspace_settings()
    permissions.reset()

    assert not config.current_settings().local_grants_trusted
    assert permissions._is_gated("bash", {"command": "tracked test"})
    assert "Bash(tracked *)" in "\n".join(shown)


def test_local_settings_git_provenance_distinguishes_untracked_and_tracked(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    (project / ".minicc").mkdir(parents=True)
    local_path = project / ".minicc" / "settings.local.json"
    local_path.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)

    assert not local_settings_are_repository_supplied(project)

    subprocess.run(
        ["git", "add", ".minicc/settings.local.json"],
        cwd=project,
        check=True,
    )

    assert local_settings_are_repository_supplied(project)


def test_cli_trust_prompt_displays_launch_directory_and_repository_identity(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    main, worktree = _linked_worktree(tmp_path)
    launch_dir = worktree / "packages" / "web"
    launch_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(launch_dir)
    shown = []
    monkeypatch.setattr(cli.ux.console, "rule", lambda: None)
    monkeypatch.setattr(
        cli.ux,
        "say",
        lambda message, **_kwargs: shown.append(str(message)),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert not cli._activate_workspace_settings()

    preview = "\n".join(shown)
    assert repr(str(launch_dir.resolve())) in preview
    assert "Trust applies to repository:" in preview
    assert repr(str(main.resolve())) in preview


def test_cli_reports_invalid_project_settings_before_trust(tmp_path, monkeypatch):
    _home, project = _settings_environment(monkeypatch, tmp_path)
    (project / ".minicc" / "settings.json").write_text("{broken")
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(config.SettingsError, match=str(project)):
        cli._activate_workspace_settings()


def test_main_checks_trust_before_loading_session(monkeypatch):
    parser = object()
    args = object()
    calls = []
    monkeypatch.setattr(cli, "_parse_startup_args", lambda: (parser, args))
    monkeypatch.setattr(
        cli,
        "_activate_workspace_settings",
        lambda: calls.append("trust") or False,
    )
    def stop_after_session(*_args, **kwargs):
        calls.append(("session", kwargs["allow_project_state"]))
        raise RuntimeError("stop after ordering check")

    monkeypatch.setattr(cli, "_init_session", stop_after_session)

    with pytest.raises(RuntimeError, match="ordering check"):
        cli._main()

    assert calls == ["trust", ("session", False)]


def test_restricted_workspace_does_not_read_project_transcripts(monkeypatch):
    parser = object()
    args = SimpleNamespace(resume="attacker-controlled", cont=True)
    monkeypatch.setattr(
        cli.sessions,
        "load",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("restricted mode must not load a transcript")
        ),
    )
    monkeypatch.setattr(
        cli.sessions,
        "latest_id",
        lambda: (_ for _ in ()).throw(
            AssertionError("restricted mode must not inspect transcript state")
        ),
    )
    monkeypatch.setattr(cli.sessions, "new_id", lambda: "fresh-session")

    assert cli._init_session(
        parser,
        args,
        allow_project_state=False,
    ) == ([], "fresh-session", False)


def test_restricted_workspace_skips_git_session_commands(tmp_path, monkeypatch):
    _settings_environment(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(
        cli,
        "build_session_context",
        lambda *, include_git: seen.append(include_git) or "context",
    )
    monkeypatch.setattr(cli, "_fire_session_start", lambda *_args: "")
    monkeypatch.setattr(
        cli,
        "_git_sha",
        lambda: (_ for _ in ()).throw(
            AssertionError("restricted startup must not invoke Git")
        ),
    )

    assert cli._session_context_with_hooks("session", "startup") == "context"
    assert cli._session_info()["commit"] == "restricted"
    assert seen == [False]


def test_llm_configuration_loads_only_the_trusted_workspace_dotenv(
    tmp_path,
    monkeypatch,
):
    from minicc import llm

    _home, project = _settings_environment(monkeypatch, tmp_path)
    config.activate(config.discover_settings().view(trusted=True))
    loaded = []
    configured_client = object()
    monkeypatch.setattr(llm, "MODEL", llm.MODEL)
    monkeypatch.setattr(llm, "CACHE_TTL", llm.CACHE_TTL)
    monkeypatch.setattr(llm, "client", None)
    monkeypatch.setattr(
        llm,
        "load_dotenv",
        lambda *, dotenv_path: loaded.append(dotenv_path),
    )
    monkeypatch.setattr(llm, "Anthropic", lambda **_kwargs: configured_client)

    llm.configure_from_settings()

    assert loaded == [project / ".env"]
    assert llm.client is configured_client


def test_llm_api_requires_runtime_configuration(monkeypatch):
    from minicc import llm

    monkeypatch.setattr(llm, "client", None)
    with pytest.raises(AssertionError, match="configure_from_settings"):
        llm.summary_runtime()


def test_llm_configuration_uses_restricted_settings_without_project_dotenv(
    tmp_path,
    monkeypatch,
):
    from minicc import llm

    _settings_environment(monkeypatch, tmp_path)
    loaded = []
    configured_client = object()
    monkeypatch.setattr(
        llm,
        "load_dotenv",
        lambda *, dotenv_path: loaded.append(dotenv_path),
    )
    monkeypatch.setattr(llm, "Anthropic", lambda **_kwargs: configured_client)

    llm.configure_from_settings()

    assert loaded == []
    assert llm.MODEL == "user-model"
    assert llm.client is configured_client
