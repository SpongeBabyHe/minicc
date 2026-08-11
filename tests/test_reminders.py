"""Tests for system-reminder injection (reminders.py) and the watch poller.

What must hold: the first prompt injects the claudeMd reminder (CC's observed
format) and the skills listing when skills exist; nothing re-injects while
sources are unchanged AND a copy is in context; a source edit re-injects; a
copy lost from history (compaction, rewind, interrupted-turn rollback)
re-injects WITHOUT any file change — the history scan is the source of truth.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from pathlib import Path

import pytest

from minicc import config, memory, reminders, skills, watch


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    home = tmp_path / "home"
    proj.mkdir()
    home.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(memory, "store_dir", lambda: home / "memories")
    config.activate(
        config.discover_settings().view(project_configuration_enabled=True)
    )
    reminders.reset()
    skills.reset("sess-r")
    yield proj, home
    reminders.reset()
    skills.reset()
    config.reset_active_settings()


def _history_with(*notes):
    return [{"role": "user", "content": n} for n in notes]


# The agent-types reminder always injects (built-ins always exist), so tests
# targeting the claudeMd reminder pick it out by its unique disclaimer line
# rather than assuming it's the only block.
_CLAUDE_DISCLAIMER = "may or may not be relevant"


def _pick(out, marker):
    hits = [t for t in out if marker in t]
    return hits[0] if hits else None


# ─── watch.Poller ─────────────────────────────────────────────────────────────

def test_poller_first_call_and_change_detection(tmp_path):
    f = tmp_path / "a.txt"
    p = watch.Poller(lambda: watch.mtime_snapshot([f]))
    assert p.changed() is True  # unseeded: first call always True
    assert p.changed() is False
    f.write_text("x")  # file APPEARS (None → mtime)
    assert p.changed() is True
    assert p.changed() is False
    os.utime(f, (1, 1))  # mtime edit
    assert p.changed() is True
    f.unlink()  # file disappears (mtime → None)
    assert p.changed() is True


# ─── claudeMd reminder ────────────────────────────────────────────────────────

def test_first_prompt_injects_claude_md_cc_format(_isolated):
    proj, _ = _isolated
    (proj / "CLAUDE.md").write_text("Use uv for everything.")
    out = reminders.for_prompt([])
    text = _pick(out, "# claudeMd")
    assert text is not None
    assert text.startswith("<system-reminder>\n") and text.endswith("</system-reminder>")
    assert "# claudeMd" in text
    assert "These instructions OVERRIDE any default behavior" in text
    assert (
        f"Contents of {proj / 'CLAUDE.md'} (project instructions, checked into "
        "the codebase):" in text
    )
    assert "Use uv for everything." in text
    assert "# currentDate\nToday's date is" in text
    assert "may or may not be relevant" in text


def test_no_claude_md_still_carries_current_date(_isolated):
    text = _pick(reminders.for_prompt([]), _CLAUDE_DISCLAIMER)
    assert "# claudeMd" not in text  # no sources → no section
    assert "# currentDate" in text  # but the date always arrives (CC parity)


def test_stable_context_injects_nothing(_isolated):
    proj, _ = _isolated
    (proj / "CLAUDE.md").write_text("rules")
    out = reminders.for_prompt([])
    # every reminder now in context + nothing changed → silence
    assert reminders.for_prompt(_history_with(*out)) == []


def test_mid_session_edit_not_reinjected_cc_parity(_isolated):
    """CC loads claudeMd "at the start of every conversation"; mid-session
    CLAUDE.md edits don't apply until the copy is otherwise lost (compact /
    /clear / restart). minicc matches — no mtime watching on this kind."""
    proj, _ = _isolated
    md = proj / "CLAUDE.md"
    md.write_text("v1")
    out = reminders.for_prompt([])
    md.write_text("v2")
    os.utime(md, (md.stat().st_atime, md.stat().st_mtime + 2))
    assert reminders.for_prompt(_history_with(*out)) == []  # edit ignored


def test_lost_copy_reinjects_fresh_from_disk(_isolated):
    """Compaction / rewind / rollback eat the reminder from history → the next
    prompt re-injects, re-reading disk at that moment (CC: "after /compact,
    Claude re-reads it from disk and re-injects it")."""
    proj, _ = _isolated
    md = proj / "CLAUDE.md"
    md.write_text("v1")
    first = _pick(reminders.for_prompt([]), _CLAUDE_DISCLAIMER)
    # history lost everything → claudeMd re-injects unchanged
    assert _pick(reminders.for_prompt([]), _CLAUDE_DISCLAIMER) == first
    md.write_text("v2")  # edit + loss → the re-injected copy is FRESH
    updated = _pick(reminders.for_prompt([]), _CLAUDE_DISCLAIMER)
    assert "v2" in updated and "v1" not in updated


# ─── skills reminder ──────────────────────────────────────────────────────────

def _install_skill(root, name, text):
    d = root / ".minicc" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text)


def test_skills_reminder_appears_and_updates(_isolated):
    proj, _ = _isolated
    _install_skill(proj, "deploy", "---\ndescription: ship it\n---\nB")
    out = reminders.for_prompt([])
    sk = [t for t in out if "Skill tool" in t]
    assert len(sk) == 1
    assert "The following skills are available for use with the Skill tool:" in sk[0]
    assert "- deploy: ship it" in sk[0]
    # a NEW skill mid-session re-injects an updated listing — no /clear needed
    _install_skill(proj, "review", "---\ndescription: review code\n---\nB")
    out2 = reminders.for_prompt(_history_with(*out))
    assert len(out2) == 1 and "- review: review code" in out2[0]


def test_memory_toggle_needs_invalidate(_isolated, monkeypatch):
    """/memory on|off flips state without touching a watched file — invalidate()
    forces the rebuild that the mtime poller can't see."""
    proj, home = _isolated
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("- [F](f.md) — hook")
    monkeypatch.setattr(memory, "_enabled", True)
    out = reminders.for_prompt([])
    assert "auto-memory" in _pick(out, _CLAUDE_DISCLAIMER)
    monkeypatch.setattr(memory, "_enabled", False)  # no file signature
    assert reminders.for_prompt(_history_with(*out)) == []  # poller can't see it
    reminders.invalidate()
    updated = _pick(reminders.for_prompt(_history_with(*out)), _CLAUDE_DISCLAIMER)
    assert "auto-memory" not in updated  # index gone from the rebuilt reminder
