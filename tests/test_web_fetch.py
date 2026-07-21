"""Unit tests for web_fetch (CC-aligned): conversion, extraction routing, cache,
redirect notice, https upgrade, caps, error contract, gating. No real network —
the _open seam and the extraction call are monkeypatched."""

import io
import os
import urllib.error

os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from minicc.tools import web_fetch as wf
from minicc import permissions


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(wf, "_CACHE", {})


@pytest.fixture
def fake_extract(monkeypatch):
    """Replace the small-model extraction; capture what it receives."""
    captured = {}

    def fake(page_text, url, prompt):
        captured.update(page=page_text, url=url, prompt=prompt)
        return f"EXTRACTED[{prompt}]"

    monkeypatch.setattr(wf, "_extract", fake)
    return captured


class _FakeResp:
    def __init__(self, body: bytes, ctype="text/html", charset="utf-8", url="https://x.test/p"):
        self._body, self._ctype, self._charset, self._url = body, ctype, charset, url

    class _H:
        def __init__(self, ctype, charset):
            self._c, self._cs = ctype, charset
        def get_content_type(self):
            return self._c
        def get_content_charset(self):
            return self._cs

    @property
    def headers(self):
        return self._H(self._ctype, self._charset)

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ─── HTML→text conversion ────────────────────────────────────────────────────
def test_html_to_text_keeps_structure_drops_noise():
    html = """
    <html><head><title>t</title><script>var x=1;</script>
    <style>.a{color:red}</style></head>
    <body><h1>Title</h1><p>Hello <b>world</b>.</p>
    <ul><li>one</li><li>two</li></ul>
    <a href="https://example.com/data">a dataset</a>
    <script>alert("skip me")</script></body></html>
    """
    text = wf._html_to_text(html)
    assert "# Title" in text
    assert "Hello world" in text
    assert "- one" in text and "- two" in text
    assert "a dataset (https://example.com/data)" in text
    assert "var x=1" not in text and "alert" not in text
    assert "color:red" not in text


def test_html_to_text_survives_malformed_html():
    assert "hi" in wf._html_to_text("<div><p>hi</div>")


def test_empty_anchor_targets_suppressed():
    html = '<a href="https://x.test/icon"><img src="i.png"></a><a href="https://x.test/real">Real</a>'
    text = wf._html_to_text(html)
    assert "https://x.test/icon" not in text
    assert "Real (https://x.test/real)" in text


# ─── the CC-aligned fetch contract ───────────────────────────────────────────
def test_fetch_extracts_with_prompt_not_raw_page(monkeypatch, fake_extract):
    """The main model gets the extractor's ANSWER, not the page (lossy by design)."""
    monkeypatch.setattr(wf, "_open", lambda req, timeout: _FakeResp(b"<h1>Doc</h1><p>secret body</p>"))
    out = wf.web_fetch("https://x.test/p", "what is the title?")
    assert "EXTRACTED[what is the title?]" in out
    assert "secret body" not in out                    # raw page does NOT pass through
    assert fake_extract["prompt"] == "what is the title?"
    assert "# Doc" in fake_extract["page"]             # extractor saw converted text


def test_http_upgrades_to_https(monkeypatch, fake_extract):
    seen = {}

    def capture(req, timeout):
        seen["url"] = req.full_url
        return _FakeResp(b"<p>ok</p>")

    monkeypatch.setattr(wf, "_open", capture)
    wf.web_fetch("http://x.test/p", "q")
    assert seen["url"] == "https://x.test/p"           # CC: auto-upgrade


def test_cache_skips_second_network_hit(monkeypatch, fake_extract):
    calls = {"n": 0}

    def counting(req, timeout):
        calls["n"] += 1
        return _FakeResp(b"<p>cached page</p>")

    monkeypatch.setattr(wf, "_open", counting)
    wf.web_fetch("https://x.test/c", "q1")
    wf.web_fetch("https://x.test/c", "q2")             # same URL, new prompt
    assert calls["n"] == 1                             # network hit once (15-min cache)


def test_cross_host_redirect_returns_notice(monkeypatch):
    def raise_redirect(req, timeout):
        raise wf._CrossHostRedirect("https://other.host/moved")

    monkeypatch.setattr(wf, "_open", raise_redirect)
    out = wf.web_fetch("https://x.test/r", "q")
    assert out.startswith("Redirect:")
    assert "https://x.test/r" in out and "https://other.host/moved" in out


def test_truncates_before_processing(monkeypatch, fake_extract):
    big = b"<p>" + b"x" * (wf.MAX_CONTENT_CHARS + 5_000) + b"</p>"
    monkeypatch.setattr(wf, "_open", lambda req, timeout: _FakeResp(big))
    wf.web_fetch("https://x.test/big", "q")
    assert len(fake_extract["page"]) <= wf.MAX_CONTENT_CHARS + 20   # capped + notice
    assert fake_extract["page"].endswith("[page truncated]")


def test_rejects_non_http_and_binary(monkeypatch):
    assert wf.web_fetch("ftp://x.test/f", "q").startswith("Error: only http(s)")
    assert wf.web_fetch("file:///etc/passwd", "q").startswith("Error: only http(s)")
    monkeypatch.setattr(wf, "_open", lambda req, timeout: _FakeResp(b"\x89PNG...", ctype="image/png"))
    assert "unsupported content type image/png" in wf.web_fetch("https://x.test/i.png", "q")


def test_fetch_error_contract(monkeypatch):
    def raise_http(req, timeout):
        raise urllib.error.HTTPError("https://x.test", 404, "nf", {}, io.BytesIO())
    monkeypatch.setattr(wf, "_open", raise_http)
    assert wf.web_fetch("https://x.test/missing", "q") == "Error: HTTP 404 fetching https://x.test/missing"

    def raise_url(req, timeout):
        raise urllib.error.URLError("dns fail")
    monkeypatch.setattr(wf, "_open", raise_url)
    assert wf.web_fetch("https://no.such.host/", "q").startswith("Error: could not fetch")


def test_extract_routes_through_llm_response(monkeypatch):
    """_extract uses llm_response (accounting/retry) with the cheap model, no tools."""
    import minicc.llm as llm_mod

    captured = {}

    class _T:
        type, text = "text", "the answer"

    class _R:
        content = [_T()]

    def fake_llm(messages, system=None, stream=True, tools=None, model=None, session_id=None):
        captured.update(system=system, tools=tools, model=model, msg=messages[0]["content"])
        return _R()

    monkeypatch.setattr(llm_mod, "llm_response", fake_llm)
    out = wf._extract("PAGE TEXT", "https://x.test", "find the thing")
    assert out == "the answer"
    assert captured["tools"] == []                     # extractor gets no tools
    from minicc.agents import EXPLORE_MODEL
    assert captured["model"] == EXPLORE_MODEL          # cheap tier
    assert "PAGE TEXT" in captured["msg"] and "find the thing" in captured["msg"]


# ─── registration + gating ───────────────────────────────────────────────────
def test_registered_but_not_for_subagents():
    from minicc import agents
    from minicc.tools import TOOLS, TOOL_HANDLERS
    assert "web_fetch" in TOOL_HANDLERS
    assert "web_fetch" in {t["name"] for t in TOOLS}
    assert "web_fetch" not in agents.resolve("explore").tools
    schema = next(t for t in TOOLS if t["name"] == "web_fetch")
    assert schema["input_schema"]["required"] == ["url", "prompt"]   # CC shape


def test_web_fetch_is_gated(monkeypatch):
    permissions.reset()
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert permissions.confirm("web_fetch", {"url": "https://x.test", "prompt": "q"}) is False
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert permissions.confirm("web_fetch", {"url": "https://x.test", "prompt": "q"}) is True
    permissions.reset()