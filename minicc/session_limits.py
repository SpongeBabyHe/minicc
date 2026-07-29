"""Claude Code-compatible per-session runaway limits.

Claude Code 2.1.212 caps WebSearch requests and subagent spawns at 200 per
session, with environment-variable overrides. Consumed counts are append-only
session events, so process restart, resume, compaction, and conversation rewind
cannot accidentally refill the budget. ``/clear`` rotates to a new session id
and therefore starts fresh counters.
"""

import os
from dataclasses import dataclass

from minicc import sessions

DEFAULT_MAX_WEB_SEARCHES = 200
DEFAULT_MAX_SUBAGENTS = 200

WEB_SEARCH_ENV = "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"
SUBAGENT_ENV = "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION"

WEB_SEARCH_COUNTER = "web_searches"
SUBAGENT_COUNTER = "subagent_spawns"


def _environment_limit(name: str, default: int) -> int:
    """Read a non-negative integer override, falling back on invalid input."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _response_web_searches(response) -> int:
    """Return billable WebSearch requests reported by one API response."""
    usage = getattr(response, "usage", None)
    server_usage = getattr(usage, "server_tool_use", None)
    count = getattr(server_usage, "web_search_requests", 0) or 0
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


@dataclass
class SessionLimits:
    """Mutable counters and configured caps for one running agent loop."""

    session_id: str | None
    web_searches: int
    subagent_spawns: int
    max_web_searches: int
    max_subagents: int

    @classmethod
    def load(cls, session_id: str | None) -> "SessionLimits":
        """Reconstruct consumed counters for ``session_id`` from its transcript."""
        totals = sessions.counter_totals(session_id) if session_id else {}
        return cls(
            session_id=session_id,
            web_searches=totals.get(WEB_SEARCH_COUNTER, 0),
            subagent_spawns=totals.get(SUBAGENT_COUNTER, 0),
            max_web_searches=_environment_limit(
                WEB_SEARCH_ENV,
                DEFAULT_MAX_WEB_SEARCHES,
            ),
            max_subagents=_environment_limit(
                SUBAGENT_ENV,
                DEFAULT_MAX_SUBAGENTS,
            ),
        )

    def tools_for_request(self, tools: list[dict]) -> list[dict]:
        """Apply remaining budgets to the schemas advertised this model turn."""
        remaining_searches = max(0, self.max_web_searches - self.web_searches)
        remaining_subagents = max(0, self.max_subagents - self.subagent_spawns)
        limited: list[dict] = []
        for schema in tools:
            name = schema.get("name")
            if name == "agent" and remaining_subagents == 0:
                continue
            if name != "web_search":
                limited.append(schema)
                continue
            if remaining_searches == 0:
                continue
            search_schema = dict(schema)
            request_cap = search_schema.get("max_uses")
            if (
                not isinstance(request_cap, int)
                or isinstance(request_cap, bool)
                or request_cap <= 0
            ):
                request_cap = remaining_searches
            search_schema["max_uses"] = min(request_cap, remaining_searches)
            limited.append(search_schema)
        return limited

    def record_response(self, response) -> None:
        """Consume and persist WebSearch requests reported by ``response``."""
        count = _response_web_searches(response)
        if count <= 0:
            return
        self.web_searches += count
        if self.session_id:
            sessions.record_counter(
                self.session_id,
                WEB_SEARCH_COUNTER,
                count,
            )

    def claim_subagent(self) -> bool:
        """Consume one subagent slot, returning False when the cap is exhausted."""
        if self.subagent_spawns >= self.max_subagents:
            return False
        self.subagent_spawns += 1
        if self.session_id:
            sessions.record_counter(
                self.session_id,
                SUBAGENT_COUNTER,
            )
        return True

    def subagent_limit_result(self) -> str:
        """Return the tool result used for a stale call after the cap was reached."""
        return (
            f"Error: session subagent limit reached ({self.max_subagents}). "
            "Use /clear to start a new session budget."
        )
