"""Resolve the distinct workspace paths used by Claude Code.

``workspace_root`` bounds project-file discovery at the current checkout,
``workspace_identity`` supplies the persisted Trust key shared by linked
worktrees, and ``local_settings_root`` locates their shared local settings.
``local_settings_are_repository_supplied`` performs the post-Trust provenance
check for local capability grants. Permission-rule anchors remain source
metadata in :mod:`minicc.config`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _find_repository_root(start_dir: Path | None = None) -> Path | None:
    """Find the nearest Git root for the workspace-boundary policy.

    This private helper only reports a filesystem fact; it does not decide the
    final project or Trust boundary. It returns the nearest ancestor containing
    a filesystem ``.git`` entry, or ``None``.

    The entry may be a directory or a worktree/submodule file. This scan does
    not interpret file contents; validated worktree canonicalization is a
    separate step.
    """
    current = (start_dir or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        marker = directory / ".git"
        if marker.exists() or marker.is_symlink():
            return directory
    return None


def _read_git_pointer(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    value = path.read_text().strip()
    if not value or any(ord(character) < 32 for character in value):
        return None
    return value


def _canonical_repository_root(repository_root: Path) -> Path:
    """Resolve a linked worktree root to its main checkout after validation."""
    try:
        git_marker = repository_root / ".git"
        pointer = _read_git_pointer(git_marker)
        if pointer is None or not pointer.startswith("gitdir:"):
            return repository_root

        git_dir = (repository_root / pointer[7:].strip()).resolve()
        common_pointer = _read_git_pointer(git_dir / "commondir")
        back_pointer = _read_git_pointer(git_dir / "gitdir")
        if common_pointer is None or back_pointer is None:
            return repository_root

        common_dir = (git_dir / common_pointer).resolve()
        if git_dir.parent.resolve() != (common_dir / "worktrees").resolve():
            return repository_root
        if (git_dir / back_pointer).resolve() != git_marker.resolve():
            return repository_root
        if common_dir.name == ".git":
            return common_dir.parent
        return common_dir
    except (OSError, UnicodeError, RuntimeError):
        return repository_root


def workspace_root(
    start_dir: Path | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return the current checkout root used as a configuration boundary.

    A linked worktree remains its own boundary so project discovery can walk
    from the launch directory to that checkout, not into the main checkout.
    """
    start = (start_dir or Path.cwd()).resolve()
    home_dir = (home or Path.home()).resolve()
    repo_root = _find_repository_root(start)
    if repo_root is None or (repo_root == home_dir and start != home_dir):
        return start
    return repo_root


def workspace_identity(
    start_dir: Path | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return the persistent project key shared by linked Git worktrees."""
    start = (start_dir or Path.cwd()).resolve()
    home_dir = (home or Path.home()).resolve()
    repository_root = _find_repository_root(start)
    if repository_root is None:
        return start
    identity = _canonical_repository_root(repository_root)
    if identity == home_dir and start != home_dir:
        return start
    return identity


def _local_settings_root_is_owned(root: Path) -> bool:
    owner_getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if owner_getter is None:
        return False
    owner = owner_getter()
    entries = [root, root / ".git"]
    minicc_dir = root / ".minicc"
    if minicc_dir.exists() or minicc_dir.is_symlink():
        entries.append(minicc_dir)
    try:
        return all(entry.lstat().st_uid == owner for entry in entries)
    except OSError:
        return False


def local_settings_root(
    start_dir: Path | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return CC's repository-wide local-settings root when ownership is safe."""
    start = (start_dir or Path.cwd()).resolve()
    identity = workspace_identity(start, home=home)
    if identity == start or not _local_settings_root_is_owned(identity):
        return start
    return identity


def _local_settings_path_is_repository_supplied(root: Path) -> bool:
    minicc_dir = root / ".minicc"
    local_settings = minicc_dir / "settings.local.json"
    if minicc_dir.is_symlink() or local_settings.is_symlink():
        return True
    nested_git = minicc_dir / ".git"
    if nested_git.exists() or nested_git.is_symlink():
        return True

    environment = os.environ.copy()
    for variable in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(variable, None)
    environment.update(GIT_LITERAL_PATHSPECS="", LC_ALL="C")
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--error-unmatch",
                "--",
                ":(icase).minicc/settings.local.json",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return not (
        result.returncode == 128
        and result.stderr.startswith("fatal: not a git repository")
    )


def local_settings_are_repository_supplied(
    start_dir: Path | None = None,
) -> bool:
    """Whether local settings could have arrived through the repository.

    This check is intended to run only after generic Workspace Trust covers the
    launch directory. Symlinks, nested Git metadata, tracked files, and
    indeterminate Git failures all fail closed as repository-supplied.
    """
    start = (start_dir or Path.cwd()).resolve()
    if start == Path.home().resolve():
        return False
    return _local_settings_path_is_repository_supplied(local_settings_root(start))
