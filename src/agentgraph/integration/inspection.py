"""Project identity, Git, and neutral work-source inspection."""

from __future__ import annotations

import os
from pathlib import Path

from agentgraph.infra import GitAdapter
from agentgraph.runtime import ProjectRegistry
from agentgraph.work import WorkSource

from .errors import RepositoryRootMismatchError
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
    return ProjectInspection(
        project.project_id,
        repository.root.name,
        repository,
        git_snapshot,
        work_snapshot,
        type(work_source).__name__,
    )
