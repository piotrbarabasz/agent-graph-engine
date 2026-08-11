"""Canonical external runtime directory layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Resolve all runtime artifacts beneath an external root."""

    root: Path

    @classmethod
    def resolve(cls, root: Path | str | None = None) -> RuntimePaths:
        """Resolve explicit root, AGENTGRAPH_HOME, or the canonical home default."""

        selected = (
            Path(root)
            if root is not None
            else Path(os.environ.get("AGENTGRAPH_HOME", Path.home() / ".agentgraph"))
        )
        return cls(selected.expanduser().resolve())

    @property
    def registry(self) -> Path:
        return self.root / "registry.json"

    @property
    def registry_lock(self) -> Path:
        return self.root / "registry.lock"

    def project(self, project_id: str) -> Path:
        return self.root / "projects" / project_id

    def project_record(self, project_id: str) -> Path:
        return self.project(project_id) / "project.json"

    def project_lock(self, project_id: str) -> Path:
        return self.project(project_id) / "project.lock"

    def lease(self, project_id: str) -> Path:
        return self.project(project_id) / "lock.json"

    def active_run(self, project_id: str) -> Path:
        return self.project(project_id) / "active-run.json"

    def run(self, project_id: str, run_id: str) -> Path:
        return self.project(project_id) / "runs" / run_id

    def initializing_run(self, project_id: str, run_id: str) -> Path:
        return self.project(project_id) / "runs" / f".initializing-{run_id}"

    def initialization_recovery(self, project_id: str) -> Path:
        return self.project(project_id) / "initialization-recovery"
