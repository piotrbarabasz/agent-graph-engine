"""Data-only change proposal boundary."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import ChangeRequest, ChangeSet


@dataclass(frozen=True, slots=True)
class ChangeProviderContext:
    """Read context and external artifact location for one provider invocation."""

    repository_root: Path
    runtime_directory: Path
    baseline_head: str
    run_id: str | None = None
    node_id: str | None = None
    node_attempt_id: str | None = None
    provider_invocation_id: str | None = None
    repair_cycle: int = 0


class ChangeProvider(Protocol):
    def propose(self, request: ChangeRequest, context: ChangeProviderContext) -> ChangeSet:
        """Return structured text changes from a read-only repository context."""
