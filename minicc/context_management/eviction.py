"""Savings-guarded local eviction of old, re-fetchable tool results.

Planning is pure and every target is revalidated before mutation.  Persistent
sessions spill original outputs and append a replay delta before changing the
working set.  A failed apply leaves messages untouched, although spill files
created before a later transcript failure may remain as harmless orphans.
"""

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from minicc import config, sessions

from .budget import _serialized_size, estimate_tokens


# Start considering old tool results at 100K, keep five recent eligible
# results, and only break the prompt cache for at least 20K net token savings.
TOOL_RESULT_EVICTION_TRIGGER_TOKENS = 100_000
TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS = 20_000
TOOL_RESULT_EVICTION_KEEP_RECENT = 5
EVICTED_MARKER = "[Old tool result content cleared]"
_TOOL_OUTPUTS_SUBDIR = ".minicc/tool_outputs"
_PERSISTED_OUTPUT_RE = re.compile(
    r"\A<persisted-output>\n"
    r"Full tool result saved to: [^\n]+\n"
    r"Use read_file on this path to inspect the full result\.\n"
    r"</persisted-output>\Z",
)

# Only re-fetchable tools are eligible. Coordination and memory results carry
# state that cannot be safely reconstructed, so they stay. web_search is a
# server tool whose encrypted result blocks sit outside this local rewrite path.
COMPACTABLE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "bash",
        "grep",
        "glob",
        "web_fetch",
        "edit_file",
        "write_file",
    }
)


@dataclass(frozen=True)
class ToolResultEdit:
    """Immutable snapshot of one planned or applied content replacement."""

    message_index: int
    block_index: int
    tool_use_id: str
    tool_name: str
    original_content: object
    replacement: str


@dataclass(frozen=True)
class EvictionPlan:
    """Pure cache-breaking edit plan computed before persistence or mutation."""

    edits: tuple[ToolResultEdit, ...] = ()
    estimated_tokens_saved: int = 0
    minimum_savings_tokens: int = 0
    first_changed_message_index: int | None = None
    invalidated_suffix_tokens: int = 0

    @property
    def count(self) -> int:
        return len(self.edits)

    def __bool__(self) -> bool:
        return bool(self.edits)


@dataclass(frozen=True)
class EvictionResult:
    """Successfully applied edits and cache-impact telemetry."""

    edits: tuple[ToolResultEdit, ...] = ()
    estimated_tokens_saved: int = 0
    first_changed_message_index: int | None = None
    invalidated_suffix_tokens: int = 0

    @property
    def count(self) -> int:
        return len(self.edits)

    @property
    def replacements(self) -> tuple[dict, ...]:
        return tuple(
            {
                "tool_use_id": edit.tool_use_id,
                "content": edit.replacement,
            }
            for edit in self.edits
        )

    def __bool__(self) -> bool:
        return bool(self.edits)


def _block_attr(block, name: str):
    """Read one content-block field from either dict or SDK object form."""
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _content_bytes(content) -> int:
    """Serialized UTF-8 size for one result payload."""
    return _serialized_size(content)


def _is_evicted_content(content) -> bool:
    """Whether a tool result already contains a local eviction marker."""
    return (
        content == EVICTED_MARKER
        or (
            isinstance(content, str)
            and _PERSISTED_OUTPUT_RE.fullmatch(content) is not None
        )
    )


def _safe_component(value: str, fallback: str) -> str:
    """Make an opaque id safe for use as one path component."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe[:48] or fallback


def _tool_result_output_path(
    session_id: str,
    tool_use_id: str,
    content,
) -> Path:
    """Return the deterministic spill path for one evicted tool result."""
    safe_session = _safe_component(session_id, "session")
    safe_tool = _safe_component(tool_use_id, "tool-result")
    digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:10]
    suffix = ".txt" if isinstance(content, str) else ".json"
    return (
        Path.cwd()
        / _TOOL_OUTPUTS_SUBDIR
        / safe_session
        / f"{safe_tool}-{digest}{suffix}"
    )


def _persisted_tool_result_marker(output_path: Path) -> str:
    """Build the in-context pointer left after a tool result is spilled.

    Kept beside ``_PERSISTED_OUTPUT_RE`` on purpose: that regex must match this
    string exactly, so the format's producer and its validator share one module.
    """
    return (
        "<persisted-output>\n"
        f"Full tool result saved to: {output_path}\n"
        "Use read_file on this path to inspect the full result.\n"
        "</persisted-output>"
    )


def _persist_tool_result_output(
    session_id: str,
    tool_use_id: str,
    content,
) -> Path:
    """Persist an evicted result outside active context and return its path."""
    config.ensure_project_dir("tool_outputs")
    output_path = _tool_result_output_path(session_id, tool_use_id, content)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        serialized = content
    else:
        serialized = json.dumps(
            content,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    output_path.write_text(serialized, encoding="utf-8")
    return output_path


def _planned_replacement(
    session_id: str | None,
    tool_use_id: str,
    content,
) -> str:
    """Return the marker shape used to estimate one candidate's net savings."""
    if not session_id:
        return EVICTED_MARKER
    output_path = _tool_result_output_path(
        session_id,
        tool_use_id,
        content,
    )
    return _persisted_tool_result_marker(output_path)


def plan_tool_result_eviction(
    messages,
    min_savings_tokens: int | None = None,
    keep_recent: int | None = None,
    session_id: str | None = None,
) -> EvictionPlan:
    """Build a mutation-free eviction plan for eligible old results.

    Results from stateful or non-re-fetchable tools are excluded.  The newest
    ``keep_recent`` eligible results remain intact, and the aggregate *net*
    saving must meet ``min_savings_tokens``.
    """
    tool_names: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _block_attr(block, "type") != "tool_use":
                continue
            tool_use_id = _block_attr(block, "id")
            tool_name = _block_attr(block, "name")
            if tool_use_id and tool_name:
                tool_names[str(tool_use_id)] = str(tool_name)

    paired_results = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            tool_name = tool_names.get(str(tool_use_id))
            if tool_name not in COMPACTABLE_TOOL_NAMES:
                continue
            paired_results.append(
                (
                    message_index,
                    block_index,
                    str(tool_use_id),
                    tool_name,
                    block.get("content"),
                )
            )

    recent_to_keep = (
        TOOL_RESULT_EVICTION_KEEP_RECENT
        if keep_recent is None
        else max(0, keep_recent)
    )
    if len(paired_results) <= recent_to_keep:
        return EvictionPlan()
    old_results = (
        paired_results
        if recent_to_keep == 0
        else paired_results[:-recent_to_keep]
    )

    edits = []
    net_bytes_saved = 0
    minimum_savings = (
        TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS
        if min_savings_tokens is None
        else max(0, min_savings_tokens)
    )
    for (
        message_index,
        block_index,
        tool_use_id,
        tool_name,
        original_content,
    ) in old_results:
        if _is_evicted_content(original_content):
            continue
        original_snapshot = deepcopy(original_content)
        replacement = _planned_replacement(
            session_id,
            tool_use_id,
            original_snapshot,
        )
        bytes_saved = (
            _content_bytes(original_snapshot)
            - _content_bytes(replacement)
        )
        if bytes_saved <= 0:
            continue
        edits.append(
            ToolResultEdit(
                message_index=message_index,
                block_index=block_index,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                original_content=original_snapshot,
                replacement=replacement,
            )
        )
        net_bytes_saved += bytes_saved

    estimated_tokens_saved = net_bytes_saved // 4
    if estimated_tokens_saved < minimum_savings or not edits:
        return EvictionPlan()
    first_changed = min(edit.message_index for edit in edits)
    return EvictionPlan(
        edits=tuple(edits),
        estimated_tokens_saved=estimated_tokens_saved,
        minimum_savings_tokens=minimum_savings,
        first_changed_message_index=first_changed,
        invalidated_suffix_tokens=estimate_tokens(messages[first_changed:]),
    )


def apply_tool_result_eviction(
    messages,
    plan: EvictionPlan,
    session_id: str | None = None,
    allow_lossy_fallback: bool = False,
) -> EvictionResult:
    """Validate, persist, and apply one fresh plan without partial mutation.

    Main-session outputs are spilled and the replay delta is logged before any
    message is changed. Normal eviction aborts if persistence fails; structural
    400/413 recovery may explicitly allow the lossy CC marker as a fallback.
    Spill files can outlive an aborted transcript write, but the working set
    remains unchanged.
    """
    if not plan:
        return EvictionResult()

    current_blocks = []
    for edit in plan.edits:
        try:
            block = messages[edit.message_index]["content"][edit.block_index]
        except (IndexError, KeyError, TypeError):
            return EvictionResult()
        if (
            not isinstance(block, dict)
            or block.get("type") != "tool_result"
            or str(block.get("tool_use_id")) != edit.tool_use_id
            or block.get("content") != edit.original_content
        ):
            return EvictionResult()
        current_blocks.append(block)

    prepared = []
    for edit in plan.edits:
        replacement = EVICTED_MARKER
        if session_id:
            try:
                output_path = _persist_tool_result_output(
                    session_id,
                    edit.tool_use_id,
                    edit.original_content,
                )
                replacement = _persisted_tool_result_marker(
                    output_path
                )
            except OSError:
                if not allow_lossy_fallback:
                    return EvictionResult()
        prepared.append(
            ToolResultEdit(
                message_index=edit.message_index,
                block_index=edit.block_index,
                tool_use_id=edit.tool_use_id,
                tool_name=edit.tool_name,
                original_content=edit.original_content,
                replacement=replacement,
            )
        )

    bytes_saved = sum(
        max(
            0,
            _content_bytes(edit.original_content)
            - _content_bytes(edit.replacement),
        )
        for edit in prepared
    )
    estimated_tokens_saved = bytes_saved // 4
    if estimated_tokens_saved < plan.minimum_savings_tokens:
        return EvictionResult()

    first_changed = min(edit.message_index for edit in prepared)
    invalidated_suffix_tokens = estimate_tokens(messages[first_changed:])
    if session_id:
        try:
            sessions.log_context_edit(
                session_id,
                tuple(
                    {
                        "tool_use_id": edit.tool_use_id,
                        "content": edit.replacement,
                    }
                    for edit in prepared
                ),
            )
        except OSError:
            return EvictionResult()

    for block, edit in zip(current_blocks, prepared):
        block["content"] = edit.replacement

    return EvictionResult(
        edits=tuple(prepared),
        estimated_tokens_saved=estimated_tokens_saved,
        first_changed_message_index=first_changed,
        invalidated_suffix_tokens=invalidated_suffix_tokens,
    )


def evict_old_tool_results(
    messages,
    min_savings_tokens: int | None = None,
    keep_recent: int | None = None,
    session_id: str | None = None,
    allow_lossy_fallback: bool = False,
) -> EvictionResult:
    """Plan and apply one guarded batch of old tool-result replacements."""
    plan = plan_tool_result_eviction(
        messages,
        min_savings_tokens=min_savings_tokens,
        keep_recent=keep_recent,
        session_id=session_id,
    )
    return apply_tool_result_eviction(
        messages,
        plan,
        session_id=session_id,
        allow_lossy_fallback=allow_lossy_fallback,
    )
