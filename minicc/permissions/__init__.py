"""Source-aware tool authorization exposed through a stable package API.

The subsystem is split by responsibility:

* :mod:`models` defines immutable decision and rule values.
* :mod:`bash` performs pure shell parsing and read-only analysis.
* :mod:`rules` loads and matches source-anchored settings rules.
* :mod:`authorization` owns ephemeral grants, prompts, and final decisions.

Approval authorizes the requested call; it does not sandbox the approved tool.
"""

from .models import AuthorizationResult, PermissionEffect, PermissionRule
from .bash import is_readonly_command
from .rules import matching_rule as _matching_rule
from .rules import permission_rules
from .authorization import (
    GATED_TOOLS,
    NO_PRELOAD,
    _ALLOWED,
    _is_gated,
    authorize,
    clear_skill_grants,
    confirm,
    derive_rules,
    filter_tools,
    grant_skill_tools,
    preload,
    reset,
)


__all__ = [
    "AuthorizationResult",
    "GATED_TOOLS",
    "NO_PRELOAD",
    "PermissionEffect",
    "PermissionRule",
    "authorize",
    "clear_skill_grants",
    "confirm",
    "derive_rules",
    "filter_tools",
    "grant_skill_tools",
    "is_readonly_command",
    "permission_rules",
    "preload",
    "reset",
]
