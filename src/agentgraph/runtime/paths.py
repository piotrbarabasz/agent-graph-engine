"""Canonical external runtime directory layout."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import RuntimePathError
from .ids import validate_project_id, validate_run_id


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Resolve all runtime artifacts beneath an external root."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

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
        validate_project_id(project_id)
        return self._contained(self.root / "projects", project_id)

    def project_record(self, project_id: str) -> Path:
        return self.project(project_id) / "project.json"

    def project_lock(self, project_id: str) -> Path:
        return self.project(project_id) / "project.lock"

    def lease(self, project_id: str) -> Path:
        return self.project(project_id) / "lock.json"

    def active_run(self, project_id: str) -> Path:
        return self.project(project_id) / "active-run.json"

    def run(self, project_id: str, run_id: str) -> Path:
        validate_run_id(run_id)
        return self._contained(self.project(project_id) / "runs", run_id)

    def initializing_run(self, project_id: str, run_id: str) -> Path:
        validate_run_id(run_id)
        return self._contained(self.project(project_id) / "runs", f".initializing-{run_id}")

    def initialization_recovery(self, project_id: str) -> Path:
        return self.project(project_id) / "initialization-recovery"

    def require_external_to(self, target_root: Path | str) -> None:
        """Reject a runtime root equal to or nested beneath a target repository."""

        target = Path(target_root).expanduser().resolve()
        runtime_key = os.path.normcase(str(self.root))
        target_key = os.path.normcase(str(target))
        try:
            contained = os.path.commonpath((runtime_key, target_key)) == target_key
        except ValueError:
            contained = False
        if contained:
            raise RuntimePathError("runtime root must be outside the target repository")

    def _contained(self, parent: Path, name: str) -> Path:
        resolved_parent = parent.resolve()
        candidate = (resolved_parent / name).resolve()
        try:
            resolved_parent.relative_to(self.root)
            candidate.relative_to(resolved_parent)
        except ValueError as exc:
            raise RuntimePathError("runtime path escapes its expected hierarchy") from exc
        return candidate
