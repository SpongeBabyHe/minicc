"""Tests for _read_query — multi-line paste support (dogfood R5 finding).

A pasted block must arrive as ONE query: input() stops at the first newline
and the rest of the paste is lost (flushed by the next prompt's stale-stdin
guard — R5 scene audit: the shattered sessions carry only the first sentence). The fix drains lines already buffered on a TTY stdin
(burst = paste); typed input and piped/scripted stdin are unaffected.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from minicc import cli


class _PipeStdin:
    """stdin stand-in: a real pipe fd (select-able) + controllable isatty."""

    def __init__(self, data: bytes, tty: bool):
        self._r, w = os.pipe()
        if data:
            os.write(w, data)
        os.close(w)  # writer closed: reads past `data` hit EOF
        self._tty = tty

    def fileno(self):
        return self._r

    def isatty(self):
        return self._tty

    def readline(self):
        buf = b""
        while True:
            ch = os.read(self._r, 1)
            if not ch or ch == b"\n":
                return (buf + ch).decode()
            buf += ch


def test_paste_burst_becomes_one_query(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", _PipeStdin(b"line2\nline3\n", tty=True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "line1")
    assert cli._read_query() == "line1\nline2\nline3"


def test_typed_line_returns_unchanged(monkeypatch):
    # nothing buffered beyond the typed line (EOF guard also exercised)
    monkeypatch.setattr(cli.sys, "stdin", _PipeStdin(b"", tty=True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "  hi  ")
    assert cli._read_query() == "hi"


def test_piped_stdin_keeps_line_per_query(monkeypatch):
    # scripted use: everything is "buffered" — draining would swallow the
    # whole script, so non-TTY stdin keeps one-line-per-query semantics
    monkeypatch.setattr(cli.sys, "stdin", _PipeStdin(b"next-command\n", tty=False))
    monkeypatch.setattr("builtins.input", lambda prompt="": "line1")
    assert cli._read_query() == "line1"


def test_surrogate_escapes_recovered_not_crashing(monkeypatch):
    """The R5 crash: input() handed back surrogate escapes and the transcript
    writer died with 'surrogates not allowed'. Escapes whose bytes form valid
    UTF-8 recover the real character; the result must always be encodable."""
    monkeypatch.setattr(cli.sys, "stdin", _PipeStdin(b"", tty=True))
    # '你' (e4 bd a0) arriving as three surrogate escapes from a mangled decode
    monkeypatch.setattr("builtins.input", lambda prompt="": "say \udce4\udcbd\udca0 ok")
    out = cli._read_query()
    assert out == "say 你 ok"
    out.encode("utf-8")  # must survive the transcript write


def test_invalid_bytes_in_drain_become_replacement(monkeypatch):
    # a truncated multi-byte sequence in the pasted burst → U+FFFD, no crash
    monkeypatch.setattr(cli.sys, "stdin", _PipeStdin(b"tail \xe4\xbd\n", tty=True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "head")
    out = cli._read_query()
    assert out.startswith("head\ntail ")
    out.encode("utf-8")
