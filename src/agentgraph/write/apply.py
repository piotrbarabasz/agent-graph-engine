"""Guarded all-or-nothing-preflight text change application."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.work import RepoPathSpec

from .capability import normalize_repo_path, path_is_allowed
from .errors import ChangePathError, StaleFileError, WorkspaceError
from .models import AppliedChangeSet, AppliedFile, ChangeSet


@dataclass(frozen=True, slots=True)
class _PreparedWrite:
    destination: Path
    relative: str
    before: str | None
    data: bytes


def apply_changeset(
    workspace: Path,
    changeset: ChangeSet,
    allowed_paths: tuple[RepoPathSpec, ...],
) -> AppliedChangeSet:
    root = workspace.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise WorkspaceError("workspace must be a real directory")
    prepared = tuple(_prepare(root, change, allowed_paths) for change in changeset.changes)
    for item in prepared:
        atomic_write_bytes(item.destination, item.data)
    files = tuple(
        AppliedFile(item.relative, item.before, _digest(item.data), len(item.data))
        for item in sorted(prepared, key=lambda value: value.relative.encode("utf-8"))
    )
    return AppliedChangeSet(files, changeset.digest)


def _prepare(root: Path, change: object, allowed: tuple[RepoPathSpec, ...]) -> _PreparedWrite:
    from .models import FileChange

    assert isinstance(change, FileChange)
    pure = normalize_repo_path(change.path)
    if not path_is_allowed(change.path, allowed):
        raise ChangePathError("out_of_scope_change")
    destination = root.joinpath(*pure.parts)
    _verify_ancestors(root, destination.parent)
    if destination.is_symlink():
        raise ChangePathError("symlink file targets are forbidden")
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ChangePathError("change path escapes workspace") from exc
    if destination.exists():
        if not destination.is_file():
            raise ChangePathError("existing change target is not a regular file")
        before = _digest(destination.read_bytes())
        if change.expected_before_sha256 is None or before != change.expected_before_sha256:
            raise StaleFileError("existing file hash differs from proposal")
    else:
        before = None
        if change.expected_before_sha256 is not None:
            raise StaleFileError("new file proposal supplied an existing-file hash")
    return _PreparedWrite(destination, change.path, before, change.content.encode("utf-8"))


def _verify_ancestors(root: Path, parent: Path) -> None:
    current = root
    for part in parent.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            try:
                current.resolve(strict=True).relative_to(root)
            except ValueError as exc:
                raise ChangePathError("symlink ancestor escapes workspace") from exc
        if current.exists() and not current.is_dir():
            raise ChangePathError("change ancestor is not a directory")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
