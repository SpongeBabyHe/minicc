"""Tests for the permission layer — read-only bash carve-out + persisted allow
rules (CC parity) + prompt-safety fixes.

The carve-out must never let a mutating command skip the prompt (false ALLOW =
security hole), while common exploration commands run free. Everything
unparseable prompts — the safe direction.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import config, permissions
from minicc.permissions import _is_gated, derive_rules, is_readonly_command


@pytest.fixture(autouse=True)
def _fresh_rules():
    permissions.reset()
    yield
    permissions.reset()


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


def test_compound_with_one_mutation_prompts():
    assert not is_readonly_command("ls && rm -rf x")       # the CC example
    assert not is_readonly_command("git status; git push")
    assert not is_readonly_command("cat f | tee out.txt")


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


# ─── wrapper stripping (CC strips timeout/time/nice/nohup/stdbuf, bare xargs) ─

def test_wrappers_stripped():
    assert is_readonly_command("timeout 30 cat big.log")
    assert is_readonly_command("nice -n 10 grep -r foo .")
    assert is_readonly_command("xargs grep pattern")       # bare xargs
    assert not is_readonly_command("xargs -n1 rm")         # flagged xargs = xargs itself
    assert not is_readonly_command("timeout 30 rm -rf x")  # wrapper hides nothing


# ─── find: read-only except exec/mutate flags ────────────────────────────────

def test_find_exec_and_delete_prompt():
    assert is_readonly_command("find . -name '*.py'")
    assert not is_readonly_command("find . -name '*.tmp' -delete")
    assert not is_readonly_command("find . -name '*.py' -exec rm {} ;")


# ─── cd: inside cwd is read-only, outside prompts ────────────────────────────

def test_cd_inside_cwd_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    assert is_readonly_command("cd sub && ls")
    assert not is_readonly_command("cd /etc && ls")
    assert not is_readonly_command("cd ~ && ls")


# ─── integration: _is_gated consults the carve-outs for bash only ────────────

def test_is_gated_bash_readonly_carveout():
    assert not _is_gated("bash", {"command": "git status"})
    assert _is_gated("bash", {"command": "pip install requests"})
    assert _is_gated("bash", {"command": "ls && rm x"})
    # other gated tools unaffected
    assert _is_gated("write_file", {"path": "x", "content": "y"})
    assert not _is_gated("memory", {"command": "view"})


# ─── persisted allow rules (CC's Bash(prefix *) semantics) ───────────────────

@pytest.fixture()
def _rules_env(tmp_path, monkeypatch):
    """Isolated settings files + fresh rule cache; returns the project path."""
    g, p = tmp_path / "global.json", tmp_path / "project" / "settings.json"
    monkeypatch.setattr(config, "_global", lambda: g)
    monkeypatch.setattr(config, "_project", lambda: p)
    permissions.reset()
    yield p
    permissions.reset()


def _set_rules(path, rules):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"allow": rules}}))
    permissions.reset()  # drop cache so the new file is read


def test_rule_wildcard_word_boundary(_rules_env):
    _set_rules(_rules_env, ["bash(uv run *)"])
    assert not _is_gated("bash", {"command": "uv run pytest tests/ -q"})
    assert not _is_gated("bash", {"command": "uv run"})       # trailing-* matches bare prefix
    assert _is_gated("bash", {"command": "uv runx"})           # word boundary holds
    assert _is_gated("bash", {"command": "uv sync"})           # different subcommand


def test_rule_exact_and_mid_wildcard_and_colon_alias(_rules_env):
    _set_rules(_rules_env, ["bash(npm test)", "bash(git * main)", "bash(make:*)"])
    assert not _is_gated("bash", {"command": "npm test"})      # exact
    assert _is_gated("bash", {"command": "npm test --watch"})  # exact ≠ prefix
    assert not _is_gated("bash", {"command": "git checkout main"})  # mid-* spans args
    assert not _is_gated("bash", {"command": "make build"})    # :* alias == " *"


def test_rule_accepts_cc_capitalized_export(_rules_env):
    _set_rules(_rules_env, ["Bash(uv run *)"])                 # CC-exported shape drops in
    assert not _is_gated("bash", {"command": "uv run pytest"})


def test_rule_compound_mix_and_fail_safe(_rules_env):
    _set_rules(_rules_env, ["bash(uv run *)"])
    # read-only + rule-matched subcommands mix freely
    assert not _is_gated("bash", {"command": "git status && uv run pytest -q"})
    # one unmatched subcommand gates the whole command
    assert _is_gated("bash", {"command": "uv run pytest && pip install x"})
    # unsafe metacharacters still gate even when a rule would match
    assert _is_gated("bash", {"command": "uv run pytest > /tmp/out"})
    # wrappers stripped before matching (CC rule); safe redirects exempt
    assert not _is_gated("bash", {"command": "timeout 300 uv run pytest 2>&1"})


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
    assert permissions.confirm("bash", {"command": "uv run pytest tests/ -q"})
    # rule live in-session without reset
    assert not _is_gated("bash", {"command": "uv run ruff check ."})
    # and persisted: survives a cache reset (re-read from the settings file)
    permissions.reset()
    assert not _is_gated("bash", {"command": "uv run pytest"})
    assert "bash(uv run *)" in _rules_env.read_text()


# ─── phantom-decline fixes ───────────────────────────────────────────────────

def test_empty_answer_reprompts_instead_of_declining(monkeypatch):
    # a buffered Enter must never decide a permission
    answers = iter(["", "", "yes"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert permissions.confirm("write_file", {"path": "x", "content": "y"})


def test_explicit_no_still_declines(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    assert not permissions.confirm("write_file", {"path": "x", "content": "y"})


def test_add_allow_rule_tolerates_malformed_settings(_rules_env):
    import json

    _rules_env.parent.mkdir(parents=True, exist_ok=True)
    _rules_env.write_text(json.dumps({"permissions": []}))  # hand-edited breakage
    config.add_allow_rule("uv run *")                       # must not crash
    data = json.loads(_rules_env.read_text())
    assert data["permissions"]["allow"] == ["bash(uv run *)"]
