"""Tests for the permission layer — read-only bash carve-out + persisted allow
rules (CC parity) + prompt-safety fixes.

The carve-out must never let a mutating command skip the prompt (false ALLOW =
security hole), while common exploration commands run free. Everything
unparseable prompts — the safe direction.
"""

import json
import os
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import config, permissions
from minicc.permissions import derive_rules, is_readonly_command


@pytest.fixture(autouse=True)
def _fresh_rules(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_args: "no")
    config.reset_active_settings()
    permissions.reset()
    yield
    permissions.reset()
    config.reset_active_settings()


def _allowed_without_approval(tool_name: str, tool_input: dict) -> bool:
    """Whether public authorization succeeds when the user declines prompts."""
    return permissions.authorize(tool_name, tool_input).allowed


# ─── plain read-only commands run free ───────────────────────────────────────

def test_simple_readonly_allowed():
    for cmd in (
        "ls -la", "cat pyproject.toml", "pwd", "head -50 src/main.py",
        "grep -rn TODO src", "wc -l README.md", "which python",
        "diff a.txt b.txt", "stat file", "du -sh .", "echo hello",
    ):
        assert is_readonly_command(cmd), cmd


def test_git_readonly_forms_allowed():
    for cmd in (
        "git status", "git log --oneline -5", "git diff HEAD~1",
        "git show abc123", "git blame src/main.py", "git rev-parse HEAD",
        "git ls-files", "git check-ignore docs",
    ):
        assert is_readonly_command(cmd), cmd


def test_git_mutating_forms_prompt():
    for cmd in (
        "git push", "git commit -m x",
        "git branch -D old",           # branch excluded entirely (has -D form)
        "git stash pop",               # stash excluded entirely
        "git checkout main",
        "git log --output=stole.txt",  # write-capable flag on a read-only subcommand
    ):
        assert not is_readonly_command(cmd), cmd


# ─── compound commands: EVERY subcommand must qualify ────────────────────────

def test_compound_all_readonly_allowed():
    assert is_readonly_command("git status && git log --oneline -3")
    assert is_readonly_command("cat a.txt | grep foo | wc -l")
    assert is_readonly_command("ls\ncat pyproject.toml")


def test_compound_with_one_mutation_prompts():
    assert not is_readonly_command("ls && rm -rf x")       # the CC example
    assert not is_readonly_command("git status; git push")
    assert not is_readonly_command("cat f | tee out.txt")
    assert not is_readonly_command("echo safe # comment\nrm -rf x")


def test_quoted_operators_not_split():
    # a quoted "&&" is data, not an operator — the command is just echo
    assert is_readonly_command('echo "a && b"')


# ─── unsafe metacharacters → prompt; harmless redirects exempt ───────────────

def test_unsafe_metacharacters_prompt():
    for cmd in (
        "git log > /tmp/f",            # redirection writes
        "echo hi >> notes.md",
        "cat $(rm -rf x)",             # command substitution executes
        "cat `rm x`",
        "diff <(ls) <(ls ..)",         # process substitution
        "ls\nrm x",                    # newline separator
        "(ls)",                        # subshell
        "PATH=/evil ls",               # env-prefix can swap the binary
    ):
        assert not is_readonly_command(cmd), cmd


def test_safe_redirects_tolerated():
    assert is_readonly_command("cat f 2>/dev/null")
    assert is_readonly_command("ls -la 2>&1")
    assert is_readonly_command("which ruff >/dev/null")
    assert is_readonly_command("grep -r foo . 2>/dev/null | head -5")


def test_file_redirects_still_gate():
    assert not is_readonly_command("cat f > out.txt")
    assert not is_readonly_command("cat f 2>err.log")
    assert not is_readonly_command("ls >> log.txt")
    assert not is_readonly_command("echo hi >/dev/null-file")


# ─── wrapper stripping ───────────────────────────────────────────────────────

def test_wrappers_stripped():
    assert is_readonly_command("timeout 30 cat big.log")
    assert is_readonly_command("nice -n 10 grep -r foo .")
    assert is_readonly_command("command cat pyproject.toml")
    assert is_readonly_command("builtin echo hello")
    assert is_readonly_command("noglob ls *.py")
    assert is_readonly_command("xargs grep pattern")       # bare xargs
    assert not is_readonly_command("xargs -n1 rm")         # flagged xargs = xargs itself
    assert not is_readonly_command("timeout 30 rm -rf x")  # wrapper hides nothing
    assert not is_readonly_command("timeout --signal ls 5 rm -rf x")
    assert is_readonly_command("timeout --signal=TERM 5 cat big.log")
    assert not is_readonly_command("stdbuf -o cat rm -rf x")


# ─── find: read-only except exec/mutate flags ────────────────────────────────

def test_find_exec_and_delete_prompt():
    assert is_readonly_command("find . -name '*.py'")
    assert not is_readonly_command("find . -name '*.tmp' -delete")
    assert not is_readonly_command("find . -name '*.py' -exec rm {} ;")
    assert not is_readonly_command("find . -fprint0 paths.txt")


# ─── cd: inside cwd is read-only, outside prompts ────────────────────────────

def test_cd_inside_cwd_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    assert is_readonly_command("cd sub && ls")
    assert is_readonly_command("cd . && git status")
    assert not is_readonly_command("cd sub && git status")
    assert not is_readonly_command("cd /etc && ls")
    assert not is_readonly_command("cd ~ && ls")


# ─── integration: authorization consults Bash carve-outs only ────────────────

def test_authorization_uses_bash_readonly_carveout():
    assert _allowed_without_approval("bash", {"command": "git status"})
    assert not _allowed_without_approval("bash", {"command": "pip install requests"})
    assert not _allowed_without_approval("bash", {"command": "ls && rm x"})
    # other gated tools unaffected
    assert not _allowed_without_approval("write_file", {"path": "x", "content": "y"})
    assert _allowed_without_approval("memory", {"command": "view"})


# ─── persisted allow rules (CC's Bash(prefix *) semantics) ───────────────────

@pytest.fixture()
def _rules_env(tmp_path, monkeypatch):
    """Isolated settings files + fresh rule cache; returns project-local path."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    user_path = tmp_path / "user.json"
    shared_project_path = project / ".minicc" / "settings.json"
    local_project_path = project / ".minicc" / "settings.local.json"
    monkeypatch.setattr(config, "_user_settings_path", lambda: user_path)
    monkeypatch.setattr(
        config, "_shared_project_settings_path", lambda: shared_project_path
    )
    monkeypatch.setattr(
        config, "_local_project_settings_path", lambda: local_project_path
    )
    config.activate(
        config.discover_settings().view(project_configuration_enabled=True)
    )
    permissions.reset()
    yield local_project_path
    permissions.reset()
    config.reset_active_settings()


def _set_rules(path, rules):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"allow": rules}}))
    config.activate(
        config.discover_settings().view(project_configuration_enabled=True)
    )
    permissions.reset()  # drop cache so the new file is read


def test_rule_wildcard_word_boundary(_rules_env):
    _set_rules(_rules_env, ["bash(uv run *)"])
    assert _allowed_without_approval("bash", {"command": "uv run pytest tests/ -q"})
    assert _allowed_without_approval("bash", {"command": "uv run"})  # trailing-* matches bare prefix
    assert not _allowed_without_approval("bash", {"command": "uv runx"})  # word boundary holds
    assert not _allowed_without_approval("bash", {"command": "uv sync"})  # different subcommand


def test_rule_exact_and_mid_wildcard_and_colon_alias(_rules_env):
    _set_rules(_rules_env, ["bash(npm test)", "bash(git * main)", "bash(make:*)"])
    assert _allowed_without_approval("bash", {"command": "npm test"})  # exact
    assert not _allowed_without_approval("bash", {"command": "npm test --watch"})  # exact ≠ prefix
    assert _allowed_without_approval("bash", {"command": "git checkout main"})  # mid-* spans args
    assert _allowed_without_approval("bash", {"command": "make build"})  # :* alias == " *"


def test_rule_accepts_cc_capitalized_export(_rules_env):
    _set_rules(_rules_env, ["Bash(uv run *)"])                 # CC-exported shape drops in
    assert _allowed_without_approval("bash", {"command": "uv run pytest"})


def test_bash_star_is_equivalent_to_bare_bash(_rules_env):
    _set_rules(_rules_env, ["Bash(*)"])

    assert _allowed_without_approval("bash", {"command": "echo $(date)"})


def test_rule_compound_mix_and_fail_safe(_rules_env):
    _set_rules(_rules_env, ["bash(uv run *)"])
    # read-only + rule-matched subcommands mix freely
    assert _allowed_without_approval("bash", {"command": "git status && uv run pytest -q"})
    # one unmatched subcommand gates the whole command
    assert not _allowed_without_approval("bash", {"command": "uv run pytest && pip install x"})
    # unsafe metacharacters still gate even when a rule would match
    assert not _allowed_without_approval("bash", {"command": "uv run pytest > /tmp/out"})
    # wrappers stripped before matching (CC rule); safe redirects exempt
    assert _allowed_without_approval("bash", {"command": "timeout 300 uv run pytest 2>&1"})


def test_broad_find_rule_does_not_autoapprove_mutating_forms(_rules_env):
    _set_rules(_rules_env, ["Bash(find *)"])

    assert _allowed_without_approval("bash", {"command": "find . -name '*.py'"})
    assert not _allowed_without_approval("bash", {"command": "find . -delete"})
    assert not _allowed_without_approval("bash", {"command": "find . -fprint0 paths.txt"})


def test_derive_rules_shapes(_rules_env):
    # >2 tokens → first two + " *"; short commands stay exact; readonly parts skipped
    assert derive_rules("uv run pytest tests/ -q") == ["uv run *"]
    assert derive_rules("npm test") == ["npm test"]
    assert derive_rules("git status && npm install foo") == ["npm install *"]
    assert derive_rules("ls | rm -rf x") == ["rm -rf *"]
    assert derive_rules("cat $(evil)") == []                   # unboundable → no always
    # R2 live case: a safe redirect must not suppress the offer
    assert derive_rules("uv run pytest tests/ -v 2>&1 | tail -30") == ["uv run *"]


def test_always_persists_and_applies(_rules_env, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "always")
    assert permissions.authorize(
        "bash", {"command": "uv run pytest tests/ -q"}
    ).allowed
    # rule live in-session without reset
    assert _allowed_without_approval("bash", {"command": "uv run ruff check ."})
    # and persisted: survives a cache reset (re-read from the settings file)
    permissions.reset()
    assert _allowed_without_approval("bash", {"command": "uv run pytest"})
    assert "bash(uv run *)" in _rules_env.read_text()


# ─── source-aware deny → ask → allow policy ─────────────────────────────────

def _activate_source_rules(
    tmp_path,
    monkeypatch,
    *,
    user=None,
    project=None,
    local=None,
    project_configuration_enabled=True,
):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    paths = {
        "user": home / ".minicc" / "settings.json",
        "project": workspace / ".minicc" / "settings.json",
        "local": workspace / ".minicc" / "settings.local.json",
    }
    for name, rules in (("user", user), ("project", project), ("local", local)):
        if rules is not None:
            paths[name].parent.mkdir(parents=True, exist_ok=True)
            paths[name].write_text(json.dumps({"permissions": rules}))
    snapshot = config.discover_settings()
    config.activate(
        snapshot.view(
            project_configuration_enabled=project_configuration_enabled
        )
    )
    permissions.reset()
    return workspace, paths


def test_restricted_workspace_keeps_project_deny_and_ask_but_delays_allow(
    tmp_path,
    monkeypatch,
):
    workspace, _paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"allow": ["Bash(uv run *)"]},
        project={
            "allow": ["Bash(npm run *)"],
            "ask": ["Read(/review/**)"],
            "deny": ["Bash(git push *)"],
        },
        project_configuration_enabled=False,
    )

    assert _allowed_without_approval("bash", {"command": "uv run pytest"})
    assert not _allowed_without_approval("bash", {"command": "npm run test"})
    assert not permissions.authorize(
        "bash",
        {"command": "git push origin"},
    ).allowed

    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "yes",
    )
    result = permissions.authorize(
        "read_file",
        {"path": str(workspace / "review" / "plan.md")},
    )
    assert result.allowed
    assert prompts and "[yes/no]" in prompts[0]
    assert "project permission rule 'Read(/review/**)'" in prompts[0]


def test_restricted_workspace_does_not_offer_project_local_always(
    tmp_path,
    monkeypatch,
):
    _workspace, paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        project_configuration_enabled=False,
    )
    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "no",
    )

    result = permissions.authorize("bash", {"command": "uv run pytest"})

    assert not result.allowed
    assert prompts and "always" not in prompts[0]
    assert not paths["local"].exists()


def test_parent_covered_workspace_does_not_offer_ineffective_always(
    tmp_path,
    monkeypatch,
):
    _workspace, paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        project_configuration_enabled=False,
    )
    snapshot = config.current_settings().snapshot
    config.activate(
        snapshot.view(
            project_configuration_enabled=True,
            shared_project_grants_enabled=False,
            local_project_grants_enabled=False,
        )
    )
    permissions.reset()
    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "no",
    )

    result = permissions.authorize("bash", {"command": "uv run pytest"})

    assert not result.allowed
    assert prompts and "always" not in prompts[0]
    assert not paths["local"].exists()


def test_rule_precedence_overrides_hook_and_skill_grants(tmp_path, monkeypatch):
    _workspace, _paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"allow": ["Bash(git push *)"]},
        project={"ask": ["Bash(git push *)"]},
        local={"deny": ["Bash(git push --force *)"]},
    )
    permissions.grant_skill_tools(["Bash(git push *)"])

    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(
            AssertionError("deny must not prompt")
        ),
    )
    denied = permissions.authorize(
        "bash",
        {"command": "git push --force origin main"},
        hook_allow=True,
    )
    assert not denied.allowed
    assert "settings.local.json" in denied.reason

    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "no",
    )
    asked = permissions.authorize(
        "bash",
        {"command": "git push origin main"},
        hook_allow=True,
    )
    assert not asked.allowed
    assert prompts and "[yes/no]" in prompts[0]
    assert "all" not in prompts[0] and "always" not in prompts[0]


def test_hook_ask_forces_a_labeled_one_time_prompt(monkeypatch):
    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "yes",
    )

    result = permissions.authorize(
        "bash",
        {"command": "git status"},
        hook_ask=True,
    )

    assert result.allowed
    assert "PreToolUse hook" in prompts[0]
    assert "[yes/no]" in prompts[0]
    assert "all" not in prompts[0] and "always" not in prompts[0]


def test_switching_to_trusted_view_invalidates_permission_rule_cache(
    tmp_path,
    monkeypatch,
):
    _workspace, _paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        project={"allow": ["Bash(npm run *)"]},
        project_configuration_enabled=False,
    )
    snapshot = config.current_settings().snapshot

    assert not _allowed_without_approval("bash", {"command": "npm run test"})
    config.activate(snapshot.view(project_configuration_enabled=True))
    assert _allowed_without_approval("bash", {"command": "npm run test"})


def test_parent_covered_view_keeps_project_allow_gated(
    tmp_path,
    monkeypatch,
):
    _workspace, _paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        project={"allow": ["Bash(npm run *)"]},
        project_configuration_enabled=False,
    )
    snapshot = config.current_settings().snapshot

    config.activate(
        snapshot.view(
            project_configuration_enabled=True,
            shared_project_grants_enabled=False,
        )
    )
    permissions.reset()

    assert config.current_settings().project_configuration_enabled
    assert not _allowed_without_approval("bash", {"command": "npm run test"})


def test_bare_deny_removes_tool_from_advertised_schema(tmp_path, monkeypatch):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        project={"deny": ["Write"]},
        project_configuration_enabled=False,
    )
    tools = [{"name": "read_file"}, {"name": "write_file"}]

    assert permissions.filter_tools(tools) == [{"name": "read_file"}]


def test_cc_tool_groups_and_all_use_rules_filter_schemas(tmp_path, monkeypatch):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        project={"deny": ["Read", "Edit", "Bash(*)", "WebSearch"]},
        project_configuration_enabled=False,
    )
    tools = [
        {"name": name}
        for name in (
            "read_file", "glob", "grep", "edit_file", "write_file", "bash",
            "web_search", "task_list",
        )
    ]

    assert permissions.filter_tools(tools) == [{"name": "task_list"}]


def test_server_tool_ask_fails_closed_instead_of_running_silently(
    tmp_path,
    monkeypatch,
):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"ask": ["WebSearch"]},
    )

    assert permissions.filter_tools([{"name": "web_search"}]) == []


def test_scoped_server_tool_policy_also_fails_closed(tmp_path, monkeypatch):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"deny": ["WebSearch(query:secret *)"]},
    )

    assert permissions.filter_tools([{"name": "web_search"}]) == []


def test_summary_requests_use_the_same_denied_tool_filter(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from minicc import llm
    from minicc.tools import TOOLS

    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"deny": ["WebSearch", "Write"]},
    )
    monkeypatch.setattr(
        llm,
        "_messages_api",
        lambda: SimpleNamespace(create=lambda **_kwargs: None),
    )

    names = {tool["name"] for tool in llm.summary_runtime().default_tools}

    assert names == {tool["name"] for tool in TOOLS} - {"web_search", "write_file"}


def test_skill_rules_match_the_schema_field_and_optional_args(tmp_path, monkeypatch):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"deny": ["Skill(deploy *)"], "ask": ["Skill(review)"]},
    )

    assert permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "skill",
        {"skill": "deploy", "args": "production"},
    )
    assert permissions._matching_rule(
        permissions.PermissionEffect.ASK,
        "skill",
        {"skill": "review", "args": ""},
    )


def test_restrictive_bash_rules_match_env_prefixes_and_newlines(
    tmp_path,
    monkeypatch,
):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"deny": ["Bash(rm *)"]},
    )

    assert permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "bash",
        {"command": "FOO=bar rm -rf tmp"},
    )
    assert permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "bash",
        {"command": "echo ok\nrm -rf tmp"},
    )
    assert permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "bash",
        {"command": "echo ok # comment\nrm -rf tmp"},
    )
    assert permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "bash",
        {"command": "time -o timing.txt rm -rf tmp"},
    )
    assert permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "bash",
        {"command": "nice -10 rm -rf tmp"},
    )
    for command in (
        "echo $(rm -rf tmp)",
        "echo `rm -rf tmp`",
        "(rm -rf tmp)",
        "{ rm -rf tmp; }",
    ):
        assert permissions._matching_rule(
            permissions.PermissionEffect.DENY,
            "bash",
            {"command": command},
        )


def test_restrictive_rules_match_scalar_parameters(tmp_path, monkeypatch):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"deny": ["Bash(timeout:600000)", "Agent(background:true)"]},
    )
    denied = permissions.PermissionEffect.DENY

    assert permissions._matching_rule(
        denied,
        "bash",
        {"command": "pytest", "timeout": 600000},
    )
    assert permissions._matching_rule(
        denied,
        "agent",
        {"prompt": "review", "background": True},
    )


def test_skill_edit_group_grants_both_file_editing_tools(tmp_path, monkeypatch):
    _activate_source_rules(tmp_path, monkeypatch)

    permissions.grant_skill_tools(["Edit"])

    assert _allowed_without_approval(
        "edit_file", {"path": "x", "old_text": "", "new_text": "y"}
    )
    assert _allowed_without_approval("write_file", {"path": "x", "content": "y"})


def test_webfetch_domain_wildcards_do_not_cross_unintended_labels(
    tmp_path,
    monkeypatch,
):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={
            "deny": [
                "WebFetch(domain:example.*)",
                "WebFetch(domain:*.trusted.test)",
            ]
        },
    )
    denied = permissions.PermissionEffect.DENY

    assert permissions._matching_rule(
        denied, "web_fetch", {"url": "https://example.org"}
    )
    assert not permissions._matching_rule(
        denied, "web_fetch", {"url": "https://example.evil.com"}
    )
    assert permissions._matching_rule(
        denied, "web_fetch", {"url": "https://a.b.trusted.test./path"}
    )
    assert not permissions._matching_rule(
        denied, "web_fetch", {"url": "https://trusted.test"}
    )
    assert not permissions._matching_rule(
        denied, "web_fetch", {"url": "https://[invalid"}
    )


def test_source_specific_path_anchors_are_preserved(tmp_path, monkeypatch):
    workspace, paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"deny": ["Read(/private/**)"]},
        project={"deny": ["Read(/project-only/**)"]},
        local={"deny": ["Read(/local-only/**)"]},
    )
    home_anchor = paths["user"].parent
    rules = permissions.permission_rules()

    assert [rule.permission_anchor for rule in rules] == [
        home_anchor,
        workspace.resolve(),
        workspace.resolve(),
    ]
    assert permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "read_file",
        {"path": str(home_anchor / "private" / "key")},
    )
    assert not permissions._matching_rule(
        permissions.PermissionEffect.DENY,
        "read_file",
        {"path": str(workspace / "private" / "key")},
    )


def test_read_and_edit_rules_cover_their_cc_tool_families(tmp_path, monkeypatch):
    workspace, _paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        project={
            "deny": ["Read(/secret/**)"],
            "ask": ["Edit(/generated/**)"],
        },
    )
    denied = permissions.PermissionEffect.DENY
    asked = permissions.PermissionEffect.ASK

    for tool_name in ("read_file", "grep", "edit_file"):
        assert permissions._matching_rule(
            denied,
            tool_name,
            {"path": str(workspace / "secret" / "value")},
        )
    assert not permissions._matching_rule(
        denied,
        "write_file",
        {"path": str(workspace / "secret" / "value")},
    )
    for tool_name in ("edit_file", "write_file"):
        assert permissions._matching_rule(
            asked,
            tool_name,
            {"path": str(workspace / "generated" / "value")},
        )


def test_scoped_write_glob_and_grep_rules_are_not_file_permission_rules(
    tmp_path,
    monkeypatch,
):
    _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={
            "deny": [
                "Write(/secret/**)",
                "Glob(/secret/**)",
                "Grep(/secret/**)",
            ]
        },
    )
    denied = permissions.PermissionEffect.DENY

    assert not permissions._matching_rule(
        denied,
        "write_file",
        {"path": "/secret/value", "content": "x"},
    )
    assert not permissions._matching_rule(
        denied,
        "glob",
        {"pattern": "/secret/**"},
    )
    assert not permissions._matching_rule(
        denied,
        "grep",
        {"pattern": "value", "path": "/secret"},
    )


def test_relative_deny_directory_and_bare_filename_follow_gitignore_depth(
    tmp_path,
    monkeypatch,
):
    workspace, _paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        user={"deny": ["Read(secrets/**)", "Read(.env)"]},
    )
    denied = permissions.PermissionEffect.DENY

    for path in (
        workspace / "secrets" / "top",
        workspace / "vendor" / "secrets" / "nested",
        workspace / ".env",
        workspace / "packages" / "app" / ".env",
    ):
        assert permissions._matching_rule(
            denied,
            "read_file",
            {"path": str(path)},
        )


def test_path_rules_distinguish_project_root_current_dir_and_recursive_globs(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    working_dir = project / "packages" / "app"
    home.mkdir()
    (project / ".git").mkdir(parents=True)
    (working_dir / ".minicc").mkdir(parents=True)
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    (working_dir / ".minicc" / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "deny": [
                        "Read(/secrets/*)",
                        "Read(/recursive/**)",
                        "Read(local/*)",
                    ]
                }
            }
        )
    )
    config.activate(
        config.discover_settings().view(project_configuration_enabled=True)
    )
    permissions.reset()

    project_rule = permissions.permission_rules()[0]
    assert project_rule.permission_anchor == working_dir.resolve()
    assert not permissions.authorize(
        "read_file",
        {"path": str(working_dir / "secrets" / "key")},
    ).allowed
    assert permissions.authorize(
        "read_file",
        {"path": str(working_dir / "secrets" / "nested" / "key")},
    ).allowed
    assert not permissions.authorize(
        "read_file",
        {"path": str(working_dir / "recursive" / "nested" / "key")},
    ).allowed
    assert not permissions.authorize(
        "read_file",
        {"path": str(working_dir / "local" / "key")},
    ).allowed


def test_path_rules_check_both_symlink_and_resolved_target(tmp_path, monkeypatch):
    workspace, _paths = _activate_source_rules(
        tmp_path,
        monkeypatch,
        project={
            "allow": ["Edit(/public/**)"],
            "deny": ["Read(/secrets/**)"],
        },
    )
    secret = workspace / "secrets" / "key"
    secret.parent.mkdir()
    secret.write_text("secret")
    public = workspace / "public"
    public.mkdir()
    link = public / "key"
    link.symlink_to(secret)

    assert not permissions.authorize(
        "read_file",
        {"path": str(link)},
    ).allowed

    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "no",
    )
    assert not permissions.authorize(
        "write_file",
        {"path": str(link), "content": "replacement"},
    ).allowed
    assert prompts


# ─── phantom-decline fixes ───────────────────────────────────────────────────

def test_empty_answer_reprompts_instead_of_declining(monkeypatch):
    # a buffered Enter must never decide a permission
    answers = iter(["", "", "yes"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert permissions.authorize(
        "write_file", {"path": "x", "content": "y"}
    ).allowed


def test_explicit_no_still_declines(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    assert not permissions.authorize(
        "write_file", {"path": "x", "content": "y"}
    ).allowed


def test_add_allow_rule_tolerates_malformed_settings(_rules_env):
    import json

    _rules_env.parent.mkdir(parents=True, exist_ok=True)
    _rules_env.write_text(json.dumps({"permissions": []}))  # hand-edited breakage
    config.add_allow_rule("uv run *")                       # must not crash
    data = json.loads(_rules_env.read_text())
    assert data["permissions"]["allow"] == ["bash(uv run *)"]
