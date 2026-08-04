"""Tests for skills — CC's SKILL.md contract (see SKILL_DESIGN.md).

What must hold: discovery precedence (personal > project, closest project dir
wins), CC's documented parse failure mode (malformed YAML → body with empty
metadata), the substitution grammar including escape semantics, shell
preprocessing being a SINGLE pass whose output is never re-scanned, and
allowed-tools grants that die at the next user prompt.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from pathlib import Path

import pytest

from minicc import config, permissions, skills
from minicc.skills import Skill, parse_frontmatter
from minicc.tools import skill as skill_tool


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh cwd + HOME + settings + registries for every test."""
    proj = tmp_path / "proj"
    home = tmp_path / "home"
    proj.mkdir()
    (proj / ".git").mkdir()
    home.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        config, "_user_settings_path", lambda: home / ".minicc" / "settings.json"
    )
    monkeypatch.setattr(
        config,
        "_shared_project_settings_path",
        lambda: proj / ".minicc" / "settings.json",
    )
    config.activate(config.discover_settings().view(trusted=True))
    skills.reset("sess-123")
    permissions.reset()
    yield proj, home
    skills.reset()
    permissions.reset()
    config.reset_active_settings()


def _install(root: Path, name: str, text: str) -> Path:
    d = root / ".minicc" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text)
    return d


# ─── frontmatter parsing ──────────────────────────────────────────────────────

def test_parse_full_frontmatter():
    meta, body = parse_frontmatter(
        "---\n"
        "name: Deploy\n"
        "description: Deploy the app\n"
        "disable-model-invocation: true\n"
        "allowed-tools: bash(git add *) write_file\n"
        "arguments: [issue, branch]\n"
        "---\n\nBody here.\n"
    )
    assert meta["name"] == "Deploy"
    assert meta["disable-model-invocation"] is True
    assert meta["allowed-tools"] == "bash(git add *) write_file"
    assert meta["arguments"] == ["issue", "branch"]
    assert body == "Body here."


def test_parse_block_list_and_quotes():
    meta, _ = parse_frontmatter(
        "---\narguments:\n- issue\n- branch\ndescription: 'quoted: colon'\n---\nb"
    )
    assert meta["arguments"] == ["issue", "branch"]
    assert meta["description"] == "quoted: colon"


def test_malformed_yaml_loads_body_with_empty_metadata():
    # CC-documented behavior: /name still works, description just missing
    meta, body = parse_frontmatter("---\n:::garbage without a key\n---\nThe body.")
    assert meta == {}
    assert body == "The body."


def test_no_frontmatter_and_unterminated_fence():
    assert parse_frontmatter("plain body") == ({}, "plain body")
    meta, body = parse_frontmatter("---\nkey: value\nno closing fence")
    assert meta == {} and "no closing fence" in body


# ─── discovery & precedence ───────────────────────────────────────────────────

def test_discovery_and_personal_overrides_project(_isolated):
    proj, home = _isolated
    _install(proj, "deploy", "---\ndescription: project one\n---\nP")
    _install(home, "deploy", "---\ndescription: personal one\n---\nH")
    _install(proj, "only-proj", "Body first paragraph.\n\nSecond.")
    found = skills.discover()
    assert found["deploy"].description == "personal one"  # personal wins
    # description falls back to the body's first paragraph (CC)
    assert found["only-proj"].description == "Body first paragraph."


def test_project_ancestor_walk_closest_wins(_isolated, monkeypatch):
    proj, _home = _isolated
    sub = proj / "pkg"
    sub.mkdir()
    _install(proj, "deploy", "---\ndescription: outer\n---\nO")
    _install(sub, "deploy", "---\ndescription: inner\n---\nI")
    monkeypatch.chdir(sub)
    config.activate(config.discover_settings().view(trusted=True))
    assert skills.lookup("deploy").description == "inner"


def test_listing_hides_model_disabled_keeps_user_hidden(_isolated):
    proj, _ = _isolated
    _install(proj, "manual", "---\ndescription: d\ndisable-model-invocation: true\n---\nB")
    _install(proj, "bg", "---\ndescription: knows\nuser-invocable: false\n---\nB")
    text = skills.listing_text()
    assert "manual" not in text  # description kept out of context entirely (CC)
    assert "bg" in text
    long = "x" * 3000
    _install(proj, "cap", f"---\ndescription: {long}\n---\nB")
    assert len(skills.lookup("cap").description) == 1536  # CC's per-entry cap


# ─── rendering: substitutions ─────────────────────────────────────────────────

def test_arguments_and_indexed_and_named(_isolated):
    proj, _ = _isolated
    d = _install(
        proj,
        "sub",
        "---\narguments: [issue, branch]\n---\n"
        'all=$ARGUMENTS zero=$0 one=$ARGUMENTS[1] issue=$issue branch=$branch path=$PATH',
    )
    out = skills.render(skills.lookup("sub"), '"hello world" second')
    assert 'all="hello world" second' in out  # $ARGUMENTS = raw string as typed
    assert "zero=hello world" in out  # shell-style quoting for indexed args
    assert "one=second" in out
    assert "issue=hello world" in out and "branch=second" in out
    assert "path=$PATH" in out  # undeclared $names stay literal
    assert str(d) not in out  # sanity: no stray expansion


def test_escape_semantics(_isolated):
    proj, _ = _isolated
    _install(proj, "esc", "price \\$1.00 and \\\\$0 end")
    out = skills.render(skills.lookup("esc"), "ARG")
    assert "price $1.00" in out  # single backslash escapes, is consumed
    assert "\\\\ARG" in out  # doubled stays AND the token expands (CC doc)


def test_claude_vars_and_unknown_braced_literal(_isolated):
    proj, _ = _isolated
    d = _install(
        proj,
        "vars",
        "dir=${CLAUDE_SKILL_DIR} proj=${CLAUDE_PROJECT_DIR} "
        "sid=${CLAUDE_SESSION_ID} effort=${CLAUDE_EFFORT}",
    )
    out = skills.render(skills.lookup("vars"))
    assert f"dir={d}" in out
    assert f"proj={Path.cwd()}" in out
    assert "sid=sess-123" in out
    assert "effort=${CLAUDE_EFFORT}" in out  # no effort levels in minicc → literal


def test_arguments_appended_when_no_placeholder(_isolated):
    proj, _ = _isolated
    _install(proj, "noph", "Fix the issue.")
    assert skills.render(skills.lookup("noph"), "123").endswith("ARGUMENTS: 123")
    _install(proj, "ph", "Fix issue $ARGUMENTS.")
    out = skills.render(skills.lookup("ph"), "123")
    assert "Fix issue 123." in out and "ARGUMENTS:" not in out
    # no args → nothing appended either way
    assert "ARGUMENTS:" not in skills.render(skills.lookup("noph"), "")


# ─── rendering: shell preprocessing ───────────────────────────────────────────

def test_inline_shell_runs_only_at_word_start(_isolated):
    proj, _ = _isolated
    _install(proj, "sh", "start !`echo one`\nmid !`echo two`\nKEY=!`echo three`")
    out = skills.render(skills.lookup("sh"))
    assert "start one" in out and "mid two" in out
    assert "KEY=!`echo three`" in out  # glued to a char → literal (CC rule)


def test_fenced_shell_block_and_single_pass(_isolated):
    proj, _ = _isolated
    _install(proj, "fence", "env:\n```!\necho a\necho b\n```\ndone")
    out = skills.render(skills.lookup("fence"))
    assert "env:\na\nb\ndone" in out  # fenced block runs as ONE script
    # single pass: output that CONTAINS a placeholder shape is not re-run —
    # the fenced script emits a literal !`echo deep` (single quotes keep the
    # backticks inert in bash), which must survive as text, never execute
    _install(proj, "emit", "```!\nprintf '%s' '!`echo deep`'\n```")
    out2 = skills.render(skills.lookup("emit"))
    assert "!`echo deep`" in out2
    assert out2.count("deep") == 1  # appears only inside the literal placeholder


def test_substitution_happens_before_shell(_isolated):
    proj, _ = _isolated
    d = _install(proj, "order", "!`echo $0 ${CLAUDE_SKILL_DIR}`")
    out = skills.render(skills.lookup("order"), "hi")
    assert "hi" in out and str(d) in out  # documented: dirs usable inside !`cmd`


def test_shell_disabled_by_policy(_isolated, monkeypatch):
    proj, _ = _isolated
    monkeypatch.setattr(config, "skill_shell_disabled", lambda: True)
    _install(proj, "off", "x !`echo run` y")
    out = skills.render(skills.lookup("off"))
    assert "[shell command execution disabled by policy]" in out
    assert "run" not in out


# ─── dedup, tool handler, user invocation ─────────────────────────────────────

def test_render_tracked_dedup(_isolated):
    proj, _ = _isolated
    _install(proj, "dup", "static body $ARGUMENTS")
    sk = skills.lookup("dup")
    _c, dup1 = skills.render_tracked(sk, "a")
    _c, dup2 = skills.render_tracked(sk, "a")
    _c, dup3 = skills.render_tracked(sk, "b")  # args changed → content differs
    assert (dup1, dup2, dup3) == (False, True, False)


def test_skill_tool_paths(_isolated):
    proj, _ = _isolated
    d = _install(proj, "go", "---\ndescription: d\n---\nDo the thing $ARGUMENTS")
    _install(proj, "manual", "---\ndisable-model-invocation: true\n---\nB")
    # CC's tool_result shape (probed live): base-dir header, blank, body
    assert skill_tool.skill("go", "now") == (
        f"Base directory for this skill: {d}\n\nDo the thing now"
    )
    assert "already loaded" in skill_tool.skill("go", "now")  # identical re-invoke
    assert skill_tool.skill("nope").startswith("Error: no skill named")
    assert "only be invoked by the user" in skill_tool.skill("manual")


def test_listing_text_and_static_tool_description(_isolated):
    proj, _ = _isolated
    assert skills.listing_text() == ""  # no skills → no reminder at all
    _install(proj, "go", "---\ndescription: does things\nargument-hint: [target]\n---\nB")
    # observed CC entry shape; argument-hint stays OUT (autocomplete-only per doc)
    assert skills.listing_text() == "- go: does things"
    assert skills.lookup("go").hint == "[target]"  # still available to /help
    # the tool description is STATIC (cache-friendly) and points at the
    # system-reminder listing instead of embedding it — CC's layout
    assert "system-reminder" in skill_tool.SCHEMA["description"]
    assert "does things" not in skill_tool.SCHEMA["description"]


def test_user_invoke_rules(_isolated):
    proj, _ = _isolated
    _install(proj, "bg", "---\nuser-invocable: false\n---\nB")
    d = _install(proj, "ok", "Run it $ARGUMENTS")
    assert skills.user_invoke("missing") is None
    assert skills.user_invoke("bg") is None  # hidden from the / entry point
    # CC's TWO-part slash expansion, byte-matched to a live probe: the
    # command-tags message and the expansion message (base-dir + body)
    assert skills.user_invoke("ok", "now") == (
        "<command-message>ok</command-message>\n"
        "<command-name>/ok</command-name>\n"
        "<command-args>now</command-args>",
        f"Base directory for this skill: {d}\n\nRun it now",
    )
    # no args → no <command-args> tag (B's /init transcript shape)
    tags, _expanded = skills.user_invoke("ok")
    assert "<command-args>" not in tags


# ─── allowed-tools grants ─────────────────────────────────────────────────────

def test_allowed_tools_grants_until_cleared(_isolated):
    proj, _ = _isolated
    _install(
        proj,
        "commit",
        "---\nallowed-tools: bash(git add *), write_file\n---\nCommit it.",
    )
    assert permissions._is_gated("bash", {"command": "git add -A"})
    assert permissions._is_gated("write_file", {"path": "x", "content": "y"})
    skills.user_invoke("commit")
    assert not permissions._is_gated("bash", {"command": "git add -A"})
    assert not permissions._is_gated("write_file", {"path": "x", "content": "y"})
    assert permissions._is_gated("bash", {"command": "git push"})  # not granted
    permissions.clear_skill_grants()  # the next user prompt
    assert permissions._is_gated("bash", {"command": "git add -A"})
    assert permissions._is_gated("write_file", {"path": "x", "content": "y"})


def test_grant_entries_split_and_dir_substitution(_isolated):
    proj, _ = _isolated
    d = _install(
        proj,
        "lint",
        "---\nallowed-tools: bash(${CLAUDE_SKILL_DIR}/scripts/lint.sh *) "
        "bash(git commit *)\n---\nLint.",
    )
    entries = skills.apply_grants(skills.lookup("lint"))
    assert entries == [f"bash({d}/scripts/lint.sh *)", "bash(git commit *)"]
    assert not permissions._is_gated(
        "bash", {"command": f"{d}/scripts/lint.sh --fix"}
    )
