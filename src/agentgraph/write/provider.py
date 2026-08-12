"""Data-only change proposal boundary."""

from typing import Protocol

from .models import ChangeRequest, ChangeSet


class ChangeProvider(Protocol):
    def propose(self, request: ChangeRequest) -> ChangeSet:
        """Return structured text changes without receiving a writable path."""
