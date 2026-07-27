"""Local tool-result eviction: plan first, then persist and apply atomically."""

import re
from copy import deepcopy
from dataclasses import dataclass

from minicc import sessions

from .budget import _serialized_size, estimate_tokens


# Start considering old tool results at 100K, keep five recent eligible
# results, and only break the prompt cache for at least 20K net token savings.
TOOL_RESULT_EVICTION_TRIGGER_TOKENS = 100_000
TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS = 20_000
TOOL_RESULT_EVICTION_KEEP_RECENT = 5
EVICTED_MARKER = "[Old tool result content cleared]"
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
    """One planned or applied tool-result content replacement."""

    message_index: int
    block_index: int
    tool_use_id: str
    tool_name: str
    original_content: object
    replacement: str


@dataclass(frozen=True)
class EvictionPlan:
    """A cache-breaking edit plan, computed before any message mutation."""

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
    """Applied edits plus telemetry needed by the caller and transcript."""

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


def _planned_replacement(
    session_id: str | None,
    tool_use_id: str,
    content,
) -> str:
    if not session_id:
        return EVICTED_MARKER
    output_path = sessions.tool_result_output_path(
        session_id,
        tool_use_id,
        content,
    )
    return sessions.persisted_tool_result_marker(output_path)


def plan_tool_result_eviction(
    messages,
    min_savings_tokens: int = TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS,
    keep_recent: int | None = None,
    session_id: str | None = None,
) -> EvictionPlan:
    """Plan eligible old-result edits without mutating messages or writing files."""
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
    minimum_savings = max(0, min_savings_tokens)
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
    """Apply one fresh plan transactionally.

    Main-session outputs are spilled and the replay delta is logged before any
    message is changed. Normal eviction aborts if persistence fails; structural
    400/413 recovery may explicitly allow the lossy CC marker as a fallback.
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
                output_path = sessions.persist_tool_result_output(
                    session_id,
                    edit.tool_use_id,
                    edit.original_content,
                )
                replacement = sessions.persisted_tool_result_marker(
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
    min_savings_tokens: int = TOOL_RESULT_EVICTION_MIN_SAVINGS_TOKENS,
    keep_recent: int | None = None,
    session_id: str | None = None,
    allow_lossy_fallback: bool = False,
) -> EvictionResult:
    """Clear eligible old results when aggregate savings justify a cache edit."""
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
