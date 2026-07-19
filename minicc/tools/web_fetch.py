"""web_fetch — Claude Code-aligned WebFetch (official tools-reference behavior):

- takes a URL **and an extraction prompt**; the page is fetched, converted to
  markdown-ish text, and a **small fast model answers the prompt against it** —
  the main model receives that answer, NOT the raw page ("lossy by design",
  a context-protection measure: big pages never enter the main window);
- HTTP URLs upgrade to HTTPS; large pages truncate BEFORE processing;
- responses cache for 15 minutes;
- a redirect to a DIFFERENT host is not followed — a notice naming both URLs is
  returned so the model can re-fetch explicitly.
"""

import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from minicc.agents import EXPLORE_MODEL   # the same cheap-model tier as sub-agents

MAX_CONTENT_CHARS = 50_000       # truncate before processing (L2 convention)
_MAX_FETCH_BYTES = 2_000_000     # stop reading the body past this
_TIMEOUT_S = 20
_CACHE_TTL_S = 15 * 60           # CC: "responses are cached for 15 minutes"
_CACHE_MAX = 20                  # bound memory; oldest evicted first
_UA = "minicc-User/0.1 (WebFetch)"          # CC pattern: UA names the agent
# CC: "an Accept header that prefers Markdown over HTML"
_ACCEPT = "text/markdown, text/html;q=0.9, text/*;q=0.8, */*;q=0.5"

_TEXTUAL = ("text/", "application/json", "application/xml", "application/xhtml")

_EXTRACT_SYSTEM = (
    "You extract information from a fetched web page. Answer the extraction "
    "request using ONLY the page content provided. Be concise and specific; "
    "quote key passages where useful. If the page does not contain the requested "
    "information, say so plainly."
)

SCHEMA = {
    "name": "web_fetch",
    "description": (
        "Fetch a URL and extract information from it. Takes the URL and a prompt "
        "describing WHAT to extract; a small fast model reads the page and returns "
        "its answer — you receive that answer, not the raw page. This is lossy by "
        "design: if the answer says the page doesn't mention something, it may only "
        "mean the prompt didn't ask — re-fetch with a more specific prompt for more. "
        "Use real URLs from the user, files, or search results — never guess URLs. "
        "HTTP upgrades to HTTPS; pages cache for 15 minutes; a redirect to a "
        "different host is returned as a notice (re-fetch the new URL explicitly). "
        "Each call needs the user's approval."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http:// or https:// URL to fetch.",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "What to extract from the page, e.g. 'summarize the main "
                    "argument' or 'list every dataset mentioned with its URL'."
                ),
            },
        },
        "required": ["url", "prompt"],
    },
}


# ─── HTML→text ────────────────────────────────────────────────────────────────
class _HTMLToText(HTMLParser):
    """Minimal HTML→text: keeps document structure (headings, paragraphs, list
    markers) and the targets of text-bearing links, drops scripts/styles/head.
    Stdlib-only on purpose (no new dependencies for one tool)."""

    _SKIP = {"script", "style", "noscript", "head", "template"}
    _BLOCK = {
        "p", "div", "section", "article", "header", "footer", "main", "aside",
        "table", "tr", "ul", "ol", "blockquote", "pre", "figure", "nav",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out = []
        self._skip_depth = 0
        self._href = None
        self._a_had_text = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._out.append("\n- ")
        elif tag == "br":
            self._out.append("\n")
        elif tag in self._BLOCK:
            self._out.append("\n\n")
        elif tag == "a":
            self._href = next((v for k, v in attrs if k == "href"), None)
            self._a_had_text = False

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a":
            # Only keep the target when the anchor had visible text — bare/icon
            # links (nav menus) would otherwise spray "(https://…)" noise lines.
            if self._href and self._a_had_text and self._href.startswith(("http://", "https://")):
                self._out.append(f" ({self._href})")
            self._href = None
        elif tag in self._BLOCK:
            self._out.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            if self._href is not None and data.strip():
                self._a_had_text = True
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out)
        # collapse intra-line whitespace; drop empty list items (suppressed icon
        # links leave bare "-" markers); then collapse runs of blank lines
        lines = [" ".join(ln.split()) for ln in raw.splitlines()]
        lines = [ln for ln in lines if ln not in ("-", "#")]
        out, blank = [], 0
        for ln in lines:
            blank = blank + 1 if not ln else 0
            if blank <= 1:
                out.append(ln)
        return "\n".join(out).strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass  # malformed HTML: return whatever was collected before the failure
    return parser.text()


# ─── fetching (same-host redirects only, CC behavior) ────────────────────────
class _CrossHostRedirect(Exception):
    def __init__(self, target: str):
        self.target = target


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow same-host redirects; surface cross-host ones to the model (CC:
    'returns a text result that names the original URL and the redirect target
    instead of following it')."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_host = urllib.parse.urlparse(req.full_url).netloc
        new_host = urllib.parse.urlparse(newurl).netloc
        if new_host and new_host != old_host:
            raise _CrossHostRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirectHandler())


def _open(req, timeout):
    """Seam for tests; routes through the same-host-redirect opener."""
    return _OPENER.open(req, timeout=timeout)


# url → (fetched_at, final_url, content_type, converted_text)
_CACHE: dict = {}


def _get_page(url: str):
    """Fetched+converted page text for `url`, via the 15-minute cache.
    Returns (final_url, content_type, text) or raises/returns error paths
    handled by the caller."""
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1], hit[2], hit[3]

    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": _ACCEPT})
    with _open(req, timeout=_TIMEOUT_S) as resp:
        ctype = resp.headers.get_content_type()
        charset = resp.headers.get_content_charset() or "utf-8"
        body = resp.read(_MAX_FETCH_BYTES)
        final_url = resp.geturl()

    if not ctype.startswith(_TEXTUAL) and ctype not in ("application/rss+xml",):
        raise ValueError(f"unsupported content type {ctype} (only text pages are supported)")

    text = body.decode(charset, errors="replace")
    if ctype in ("text/html", "application/xhtml+xml"):
        text = _html_to_text(text)
    if len(text) > MAX_CONTENT_CHARS:                 # truncate BEFORE processing
        text = text[:MAX_CONTENT_CHARS] + "\n[page truncated]"

    if len(_CACHE) >= _CACHE_MAX:                     # bound memory: drop oldest
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]))
    _CACHE[url] = (time.time(), final_url, ctype, text)
    return final_url, ctype, text


def _extract(page_text: str, url: str, prompt: str) -> str:
    """CC's lossy-by-design step: a small fast model answers `prompt` against the
    page; the MAIN model gets this answer, not the page. Routed through
    llm_response so usage/accounting/retry behave like every other call."""
    from minicc.llm import llm_response   # lazy: avoid tools↔llm import cycle

    msg = f"<page url={url}>\n{page_text}\n</page>\n\nExtraction request: {prompt}"
    resp = llm_response(
        [{"role": "user", "content": msg}],
        system=_EXTRACT_SYSTEM,
        stream=False,
        tools=[],                          # the extractor gets no tools
        model=EXPLORE_MODEL,               # cheap tier, like sub-agents
    )
    answer = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    return answer or "(the extraction model returned no text)"


def web_fetch(url: str, prompt: str) -> str:
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]      # CC: auto-upgrade to HTTPS
    if not url.startswith("https://"):
        return f"Error: only http(s) URLs are supported, got: {url}"

    try:
        final_url, ctype, text = _get_page(url)
    except _CrossHostRedirect as e:
        return (
            f"Redirect: {url} redirects to a different host: {e.target}. "
            "Not followed automatically — fetch the new URL directly if appropriate."
        )
    except urllib.error.HTTPError as e:
        return f"Error: HTTP {e.code} fetching {url}"
    except urllib.error.URLError as e:
        return f"Error: could not fetch {url}: {getattr(e, 'reason', e)}"
    except ValueError as e:
        return f"Error: {e}"
    except OSError as e:  # timeouts, connection resets
        return f"Error: could not fetch {url}: {e}"

    if not text:
        return f"Error: {final_url} returned an empty page"
    answer = _extract(text, final_url, prompt)
    return f"[fetched {final_url} ({ctype}); a fast model answered the prompt against the page]\n\n{answer}"
