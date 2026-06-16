"""All terminal rendering primitives. """

from contextlib import contextmanager
from rich.console import Console
from rich.spinner import Spinner
from rich.live import Live
from rich.markdown import Markdown
from rich.theme import Theme
from rich.style import Style
import difflib
from rich.syntax import Syntax


# ---- markdown theme ----
# rich's default `markdown.code` gives inline `code` a filled background — the
# ugly dark chips. Override to a single accent color with NO background. Other
# markdown.* keys inherit rich defaults.
_CODE_ACCENT = "#0a7ea4"  # teal; readable on light terminals. Tweak to taste.
MD_THEME = Theme(
    {
        "markdown.code": Style(color=_CODE_ACCENT, bold=False),  # inline `code`, no bg
        "markdown.item.bullet": Style(color=_CODE_ACCENT, bold=True),
        "markdown.h1": Style(bold=True),
        "markdown.h2": Style(bold=True),
        "markdown.h3": Style(bold=True),
        "markdown.h4": Style(bold=True),
    }
)

# Syntax theme for fenced ``` code blocks ```. ansi_light adapts to the
# terminal palette (no forced background) — pairs with light terminals.
_CODE_BLOCK_THEME = "ansi_light"

console = Console(highlight=False, theme=MD_THEME)

# ---- style constants ----
S_USER = "bold"
S_ASSISTANT = "bold"
S_CALL = "dim"
S_RESULT = "dim"
S_ERROR = "red"
S_INFO = "dim"


def _md(text: str) -> Markdown:
    """Markdown with minicc's consistent code-block theme. Inline-code style
    comes from the console's MD_THEME (no background)."""
    return Markdown(text, code_theme=_CODE_BLOCK_THEME)


@contextmanager
def thinking(text: str = "thinking..."):
    """Wrap an LLM call: shows spinner until block exits."""
    with Live(Spinner("dots", text=text), console=console, refresh_per_second=10, transient=True):
        yield


@contextmanager
def streaming(text: str = "thinking..."):
    """Spinner until the first token, then live-rendered markdown.

    Yields a `render(delta)` callable. A spinner shows until the first delta
    (covering connection + first-token latency); then a Live region re-renders
    the accumulated text as markdown as it grows. On exit the final markdown is
    printed permanently. A tool-only turn (no text) just drops the spinner.

    Markdown (not raw print) so **bold**, lists, and code blocks render — and
    model output like "[INFO]" is treated as text, not rich markup.
    """
    live = Live(Spinner("dots", text=text), console=console,
                refresh_per_second=10, transient=True)
    live.start()
    acc = {"text": "", "started": False}

    def render(delta: str):
        acc["started"] = True
        acc["text"] += delta
        live.update(_md(acc["text"]))

    try:
        yield render
    finally:
        live.stop()  # clears the transient region (spinner, or the live markdown)
        if acc["started"]:
            # re-print the final markdown permanently (the transient region is gone)
            console.print(_md(acc["text"]))


def say(text: str, style: str = ""):
    """Print text with optional style."""
    console.print(text, style=style)


def truncate(s, n: int) -> str:
    s = str(s)
    if len(s) <= n:
        return s
    out = s[:n] + f"\n...[+{len(s) - n} more chars]"
    return out


def fmt_dict(d: dict, value_cap: int = 80) -> str:
    """Render a dict as 'k=v, k=v' with each value truncated."""
    parts = []
    for k, v in d.items():
        s = repr(v)
        if len(s) > value_cap:
            s = s[:value_cap] + f"...[+{len(s) - value_cap}]"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def headed(label: str, body: str, label_style: str = "", body_style: str = ""):
    """Print a styled label followed by body. Use for section headers."""
    say(label, style=label_style)
    say(body, style=body_style)


def kv_block(items, indent: str = "  ") -> str:
    """Render key-value rows. Multi-line values get their own indented block."""
    lines = []
    for k, v in items:
        v = str(v)
        if "\n" in v or len(v) > 60:
            lines.append(f"{indent}{k}:")
            for line in v.splitlines():
                lines.append(f"{indent}  {line}")
        else:
            lines.append(f"{indent}{k}: {v}")
    return "\n".join(lines)


def markdown(text: str):
    """Render text as markdown."""
    console.print(_md(text))


def diff_view(old: str, new: str, path: str = ""):
    def _lines(s):
        return [line + "\n" for line in s.splitlines()] or [""]

    diff = "".join(difflib.unified_diff(
        _lines(old),
        _lines(new),
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
        n=2,
    ))
    return Syntax(
        diff or "(no change)",
        "diff",
        theme="ansi_light",
        background_color="default",
    )
