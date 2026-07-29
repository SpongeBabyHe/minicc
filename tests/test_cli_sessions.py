"""CLI session selection must not confuse failures with empty conversations."""

import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc import cli, sessions


def test_resume_missing_session_exits_with_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["minicc", "--resume", "missing"])

    with pytest.raises(SystemExit) as error:
        cli._init_session()

    assert error.value.code == 2
    assert "session 'missing' was not found" in capsys.readouterr().err


def test_resume_corrupt_session_exits_with_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / ".minicc" / "sessions" / "broken.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("not-json\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["minicc", "--resume", "broken"])

    with pytest.raises(SystemExit) as error:
        cli._init_session()

    assert error.value.code == 2
    assert "is corrupt at line 1" in capsys.readouterr().err


def test_resume_valid_empty_session_is_still_resume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    transcript = tmp_path / ".minicc" / "sessions" / "empty.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"")
    monkeypatch.setattr(sys, "argv", ["minicc", "--resume", "empty"])

    assert cli._init_session() == ([], "empty", True)


def test_new_session_reports_startup_not_resume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["minicc"])

    history, session_id, resumed = cli._init_session()

    assert history == []
    assert resumed is False
    assert sessions.validate_id(session_id) == session_id
