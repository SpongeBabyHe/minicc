"""Resolve canonical repository and Trust boundaries without invoking Git."""

from __future__ import annotations

from pathlib import Path


def repository_root(start_dir: Path | None = None) -> Path | None:
    """Return the nearest ancestor containing a filesystem ``.git`` entry.

    The entry may be a directory or a worktree/submodule file. Its contents are
    deliberately not followed, so repository-controlled ``commondir`` data
    cannot redirect workspace identity before Trust is established.
    """
    current = (start_dir or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        marker = directory / ".git"
        if marker.exists() or marker.is_symlink():
            return directory
    return None


def workspace_root(
    start_dir: Path | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return the repository root.

    A launch below a HOME-level repository remains isolated by launch directory.
    """
    start = (start_dir or Path.cwd()).resolve()
    user_home = (home or Path.home()).resolve()
    repo_root = repository_root(start)
    if repo_root is None or (repo_root == user_home and start != user_home):
        return start
    return repo_root
