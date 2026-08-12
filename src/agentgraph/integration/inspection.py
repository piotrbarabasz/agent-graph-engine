"""Project identity, Git, and neutral work-source inspection."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from agentgraph.infra import GitAdapter
from agentgraph.runtime import ProjectRegistry
from agentgraph.work import WorkSource, WorkSourceRevision

from .errors import RepositoryRootMismatchError, WorkSourceRepositoryMismatchError
from .models import ProjectInspection


def inspect_project(
    repository_root: Path | str,
    *,
    git_adapter: GitAdapter,
    project_registry: ProjectRegistry,
    work_source: WorkSource,
) -> ProjectInspection:
    """Prepare one immutable repository/work snapshot after exact-root discovery."""

    configured_root = Path(repository_root).expanduser().resolve()
    repository = git_adapter.discover_repository(configured_root)
    if os.path.normcase(str(repository.root)) != os.path.normcase(str(configured_root)):
        raise RepositoryRootMismatchError("configured target must equal the canonical Git root")
    project = project_registry.register(repository.root)
    git_snapshot = git_adapter.snapshot(repository)
    work_snapshot = work_source.snapshot()
    verify_work_source_revision(repository.root, work_snapshot.revision)
    return ProjectInspection(
        project.project_id,
        repository.root.name,
        repository,
        git_snapshot,
        work_snapshot,
        type(work_source).__name__,
    )


def verify_work_source_revision(
    repository_root: Path | str,
    revision: WorkSourceRevision,
) -> None:
    """Verify every declared source document against one canonical repository root."""

    root = Path(repository_root).expanduser().resolve()
    for document in revision.documents:
        relative = _safe_revision_path(document.path)
        candidate = (root / Path(*relative.parts)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise WorkSourceRepositoryMismatchError(
                "work-source document escapes the configured repository"
            ) from exc
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise WorkSourceRepositoryMismatchError(
                "work-source document is not a readable target file"
            ) from exc
        if not candidate.is_file():
            raise WorkSourceRepositoryMismatchError(
                "work-source document is not a regular target file"
            )
        if len(raw) != document.size_bytes:
            raise WorkSourceRepositoryMismatchError("work-source document size does not match")
        digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if digest != document.sha256:
            raise WorkSourceRepositoryMismatchError("work-source document digest does not match")


def _safe_revision_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise WorkSourceRepositoryMismatchError(
            "work-source document path is not safe repository-relative POSIX"
        )
    pure = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        pure.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
        or any(part in {"", "."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise WorkSourceRepositoryMismatchError(
            "work-source document path is not safe repository-relative POSIX"
        )
    return pure
