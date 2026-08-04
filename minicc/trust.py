"""Workspace Trust, evaluated before project configuration runs.

One Trust decision covers the nearest Git root; outside a repository, the
canonical launch directory is the identity. Project settings, hooks, skills,
agents, and instructions become active only after that identity is accepted.

Trust for the user's home directory is session-only, matching Claude Code's
documented behavior.  Starting from a project below HOME is persisted normally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from minicc.workspace import workspace_root


class TrustError(ValueError):
    """The user-owned workspace Trust store is malformed."""


class TrustManager:
    """Read and persist Trust decisions for canonical workspace identities."""

    def __init__(self, store_path: Path | None = None, home: Path | None = None):
        self.home = (home or Path.home()).resolve()
        self.store_path = store_path or self.home / ".minicc" / "trust.json"
        self._session_trusted: set[Path] = set()

    def workspace_identity(self, launch_dir: Path | None = None) -> Path:
        """Return the Git root, or the canonical directory outside a repository."""
        return workspace_root(launch_dir, home=self.home)

    def _read(self) -> dict:
        if not self.store_path.exists():
            return {"trusted_workspaces": []}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TrustError(
                f"invalid JSON in workspace Trust store {self.store_path}: {error.msg}"
            ) from error
        if not isinstance(data, dict) or not isinstance(
            data.get("trusted_workspaces", []), list
        ):
            raise TrustError(
                f"workspace Trust store must contain a trusted_workspaces array: "
                f"{self.store_path}"
            )
        return data

    def is_trusted(self, launch_dir: Path | None = None) -> bool:
        """Whether ``launch_dir`` was accepted this session or a previous one."""
        identity = self.workspace_identity(launch_dir)
        if identity in self._session_trusted:
            return True
        return str(identity) in self._read().get("trusted_workspaces", [])

    def accept(self, launch_dir: Path | None = None) -> None:
        """Trust ``launch_dir`` now, persisting it unless it is HOME itself."""
        identity = self.workspace_identity(launch_dir)
        self._session_trusted.add(identity)
        if identity == self.home:
            return

        data = self._read()
        workspaces = data.setdefault("trusted_workspaces", [])
        key = str(identity)
        if key in workspaces:
            return
        workspaces.append(key)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(data, indent=2) + "\n",
            encoding="utf-8",
        )

    def ensure_trusted(
        self,
        launch_dir: Path,
        confirm: Callable[[Path], bool],
    ) -> bool:
        """Return true for existing Trust or after ``confirm`` accepts it."""
        identity = self.workspace_identity(launch_dir)
        if self.is_trusted(identity):
            return True
        if not confirm(identity):
            return False
        self.accept(identity)
        return True
