"""Protocol for deterministic immutable work-source adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    ScopeSelection,
    WorkItem,
    WorkPackage,
    WorkScope,
    WorkSelection,
    WorkSourceSnapshot,
    WorkSourceValidation,
)


@runtime_checkable
class WorkSource(Protocol):
    def validate(self) -> WorkSourceValidation: ...

    def snapshot(self) -> WorkSourceSnapshot: ...

    def get_scope(self, snapshot: WorkSourceSnapshot, scope_id: str) -> WorkScope: ...

    def get_item(self, snapshot: WorkSourceSnapshot, item_id: str) -> WorkItem: ...

    def next_ready_item(self, snapshot: WorkSourceSnapshot, scope_id: str) -> WorkSelection: ...

    def next_ready_scope(
        self, snapshot: WorkSourceSnapshot, parent_scope_id: str
    ) -> ScopeSelection: ...

    def build_package(self, snapshot: WorkSourceSnapshot, item_id: str) -> WorkPackage: ...
