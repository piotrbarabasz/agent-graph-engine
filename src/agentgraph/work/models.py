"""Immutable adapter-neutral work-source models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class WorkItemStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


class WorkScopeStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    REVIEW = "review"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class WorkRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationOrigin(StrEnum):
    ITEM = "item"
    SCOPE = "scope"


class SelectionKind(StrEnum):
    READY = "ready"
    SCOPE_COMPLETE = "scope_complete"
    BLOCKED_DEPENDENCIES = "blocked_dependencies"
    EMPTY_SCOPE = "empty_scope"


@dataclass(frozen=True, slots=True)
class SourceLocation:
    path: str
    line: int | None = None

    def __post_init__(self) -> None:
        pure = PurePosixPath(self.path)
        if (
            not self.path
            or "\x00" in self.path
            or "\\" in self.path
            or pure.is_absolute()
            or ".." in pure.parts
        ):
            raise ValueError("source location path must be repository-relative POSIX")
        if self.line is not None and self.line < 1:
            raise ValueError("source location line must be positive")


@dataclass(frozen=True, slots=True)
class SourceDocumentRevision:
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class WorkSourceRevision:
    documents: tuple[SourceDocumentRevision, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkSourceIssue:
    code: str
    severity: IssueSeverity
    message: str
    source_location: SourceLocation
    scope_id: str | None = None
    item_id: str | None = None

    def sort_key(self) -> tuple[object, ...]:
        return (
            self.source_location.path,
            self.source_location.line or 0,
            self.code,
            self.scope_id or "",
            self.item_id or "",
            self.message,
        )


@dataclass(frozen=True, slots=True)
class WorkSourceValidation:
    issues: tuple[WorkSourceIssue, ...] = ()

    @classmethod
    def from_issues(cls, issues: list[WorkSourceIssue]) -> WorkSourceValidation:
        return cls(tuple(sorted(issues, key=WorkSourceIssue.sort_key)))

    @property
    def ok(self) -> bool:
        return not any(issue.severity is IssueSeverity.ERROR for issue in self.issues)


@dataclass(frozen=True, slots=True)
class RepoPathSpec:
    path: str
    directory_hint: bool = False


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    argv: tuple[str, ...]
    raw: str
    origin: ValidationOrigin
    source_location: SourceLocation


@dataclass(frozen=True, slots=True)
class SourcePolicyHints:
    one_pr_per_scope: bool
    merge_requires_human: bool
    auto_merge: bool
    one_commit_per_item: bool
    commit_requires_human: bool
    auto_commit: bool


@dataclass(frozen=True, slots=True)
class WorkItem:
    item_id: str
    title: str
    scope_id: str
    parent_scope_id: str | None
    status: WorkItemStatus
    risk: WorkRisk
    goal: str
    acceptance_criteria: tuple[str, ...]
    test_requirements: tuple[str, ...]
    dependencies: tuple[str, ...]
    implementation_paths: tuple[RepoPathSpec, ...]
    test_paths: tuple[RepoPathSpec, ...]
    validation_checks: tuple[ValidationCheck, ...]
    final_review_required: bool
    parallelizable: bool
    notes: tuple[str, ...]
    source_location: SourceLocation


@dataclass(frozen=True, slots=True)
class WorkScope:
    scope_id: str
    parent_scope_id: str | None
    title: str
    status: WorkScopeStatus
    risk: WorkRisk | None
    goal: str
    child_scope_ids: tuple[str, ...]
    item_ids: tuple[str, ...]
    dependencies: tuple[str, ...]
    required_checks: tuple[ValidationCheck, ...]
    policy_hints: SourcePolicyHints | None
    branch_hint: str | None
    base_branch_hint: str | None
    source_location: SourceLocation


@dataclass(frozen=True, slots=True)
class WorkSourceSnapshot:
    scopes: tuple[WorkScope, ...]
    items: tuple[WorkItem, ...]
    revision: WorkSourceRevision
    active_scope_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkSelection:
    kind: SelectionKind
    scope_id: str
    item_id: str | None = None
    blocking_item_ids: tuple[str, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True, slots=True)
class ScopeSelection:
    kind: SelectionKind
    parent_scope_id: str
    scope_id: str | None = None
    blocking_scope_ids: tuple[str, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True, slots=True)
class WorkPackage:
    item_id: str
    title: str
    scope_id: str
    parent_scope_id: str | None
    risk: WorkRisk
    goal: str
    acceptance_criteria: tuple[str, ...]
    test_requirements: tuple[str, ...]
    dependencies: tuple[str, ...]
    implementation_paths: tuple[RepoPathSpec, ...]
    test_paths: tuple[RepoPathSpec, ...]
    allowed_paths: tuple[RepoPathSpec, ...]
    item_validation_checks: tuple[ValidationCheck, ...]
    scope_required_checks: tuple[ValidationCheck, ...]
    final_review_required: bool
    parallelizable: bool
    notes: tuple[str, ...]
    branch_hint: str | None
    base_branch_hint: str | None
    source_location: SourceLocation
    source_revision: WorkSourceRevision
