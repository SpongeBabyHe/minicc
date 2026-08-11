"""Tests for agent definitions — CC's subagent system (.minicc/agents/*.md).

Pins the definition contract: discovery + precedence, frontmatter parsing
(tools allowlist with CC-name aliasing, model inherit), built-in types, a
definition overriding a built-in, and the agent-types system-reminder.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from pathlib import Path

import pytest

from minicc import agents, config


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    proj, home = tmp_path / "proj", tmp_path / "home"
    proj.mkdir(); home.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    config.activate(
        config.discover_settings().view(project_configuration_enabled=True)
    )
    yield proj, home
    config.reset_active_settings()


def _install(root: Path, name: str, text: str) -> Path:
    d = root / ".minicc" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)
    return d


# ─── built-ins ────────────────────────────────────────────────────────────────

def test_builtins_available_without_files():
    assert agents.resolve(None).name == "general-purpose"   # omitted → catch-all
    assert agents.resolve("explore").tools == ["read_file", "glob", "grep"]
    assert agents.resolve("explore").model == agents.EXPLORE_MODEL
    assert agents.resolve("general-purpose").tools is None   # None = all tools
    assert agents.resolve("general-purpose").model is None   # None = inherit


def test_unknown_type_resolves_none():
    assert agents.resolve("nope") is None


# ─── definition parsing ───────────────────────────────────────────────────────

def test_definition_parsed_with_tools_alias_and_model(_isolated):
    proj, _ = _isolated
    _install(proj, "reviewer",
             "---\ndescription: reviews code\ntools: Read, Grep, Bash\nmodel: opus\n"
             "---\nYou are a strict reviewer.")
    a = agents.resolve("reviewer")
    assert a.description == "reviews code"
    assert a.prompt == "You are a strict reviewer."
    assert a.tools == ["read_file", "grep", "bash"]   # CC names aliased to minicc
    assert a.model == "opus"


def test_no_frontmatter_uses_stem_and_defaults(_isolated):
    proj, _ = _isolated
    _install(proj, "helper", "Just do helpful things.")
    a = agents.resolve("helper")
    assert a.name == "helper" and a.tools is None and a.model is None
    assert a.prompt == "Just do helpful things."


def test_model_inherit_is_none(_isolated):
    proj, _ = _isolated
    _install(proj, "x", "---\nmodel: inherit\n---\nbody")
    assert agents.resolve("x").model is None


# ─── precedence / override ────────────────────────────────────────────────────

def test_personal_overrides_project(_isolated):
    proj, home = _isolated
    _install(proj, "dup", "---\ndescription: project\n---\nP")
    _install(home, "dup", "---\ndescription: personal\n---\nH")
    assert agents.resolve("dup").description == "personal"


def test_definition_overrides_builtin(_isolated):
    proj, _ = _isolated
    _install(proj, "explore", "---\ndescription: my explore\ntools: read_file\n---\nCustom.")
    a = agents.resolve("explore")
    assert a.description == "my explore" and a.prompt == "Custom."
    assert a.tools == ["read_file"]  # the custom def, not the built-in


# ─── listing (feeds the system-reminder) ──────────────────────────────────────

def test_listing_includes_builtins_and_definitions(_isolated):
    proj, _ = _isolated
    _install(proj, "custom", "---\ndescription: does X\n---\nB")
    listing = agents.listing_text()
    assert "- general-purpose:" in listing
    assert "- explore:" in listing
    assert "- custom: does X" in listing


def test_agents_reminder_injected(_isolated):
    from minicc import reminders
    reminders.reset()
    out = reminders.for_prompt([])
    agent_reminder = [t for t in out if "Available agent types for the Agent tool:" in t]
    assert len(agent_reminder) == 1
    assert "- general-purpose:" in agent_reminder[0]
