"""web_search — Anthropic's SERVER-side web search tool (the CC-faithful choice:
the official tools-reference states CC's WebSearch runs on this backend and "is
not configurable").

Unlike every other minicc tool, there is NO client handler: the tools entry below
is the entire integration. The API runs the searches during the request and the
assistant's content comes back with `server_tool_use` + `web_search_tool_result`
blocks (results carry `encrypted_content` that MUST be passed back unchanged on
later turns — minicc's append-only history + model_dump serialization already
preserve blocks verbatim). Long search turns may pause with stop_reason
"pause_turn"; the agent loop resends the paused assistant message to continue.

GA, no beta header. Priced at $10 per 1,000 searches + token costs (tracked in
/cost). Basic `web_search_20250305` on purpose: the newer dynamic-filtering
versions route through the code-execution tool, which minicc doesn't have.

Org-level note: if web search is disabled in the org's Console settings, ANY
request that includes this tool 400s — set `"web_search": false` in
user, project, or local settings to drop it from the tool set.
"""

# Cap searches per request. Official guidance: simple lookups use 1-3, research
# 15-20; 8 matches CC's documented "up to eight backend searches per call".
SCHEMA = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 8,
}
