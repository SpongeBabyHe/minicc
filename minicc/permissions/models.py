"""Immutable value objects shared by the authorization subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from minicc.config import SettingsSource


class PermissionEffect(str, Enum):
    """A settings rule outcome, evaluated in deny, ask, allow order."""

    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


@dataclass(frozen=True)
class PermissionRule:
    """One parsed settings rule and its source-specific path interpretation."""

    effect: PermissionEffect
    raw: str
    tool_pattern: str
    argument_pattern: str | None
    source: SettingsSource
    compiled: re.Pattern | None = field(default=None, repr=False, compare=False)

    @property
    def permission_anchor(self) -> Path:
        """Base directory for a leading-slash permission path pattern."""
        return self.source.permission_anchor


@dataclass(frozen=True)
class AuthorizationResult:
    """Final decision for one tool call after policy and optional user review."""

    allowed: bool
    reason: str | None = None
