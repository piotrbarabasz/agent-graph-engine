"""Immutable source-neutral models for one read-only graph probe."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agentgraph.core import GraphState
from agentgraph.infra import GitRepository, RepositorySnapshot
from agentgraph.work import WorkPackage, WorkSourceSnapshot

from .errors import ShadowRequestError


class IntegrationIssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class BranchDisposition(StrEnum):
    ALIGNED = "aligned"
    PREPARATION_REQUIRED = "preparation_required"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"


class SelectionDisposition(StrEnum):
    READY = "ready"
    NO_WORK = "no_work"
    BLOCKED = "blocked"
    SELECTION_REQUIRED = "selection_required"


class ShadowOutcome(StrEnum):
    READY_FOR_EXPLORE = "ready_for_explore"
    NO_WORK = "no_work"
    BLOCKED = "blocked"
    INVALID_SOURCE = "invalid_source"
    INVALID_PROJECT = "invalid_project"
    SELECTION_REQUIRED = "selection_required"
    DRIFTED = "drifted"


@dataclass(frozen=True, slots=True)
class IntegrationIssue:
    code: str
    severity: IntegrationIssueSeverity
    message: str
    related_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShadowRequest:
    scope_id: str | None = None
    parent_scope_id: str | None = None

    def __post_init__(self) -> None:
        if self.scope_id is not None and (not self.scope_id or not isinstance(self.scope_id, str)):
            raise ShadowRequestError("scope_id must be a non-empty string or None")
        if self.parent_scope_id is not None and (
            not self.parent_scope_id or not isinstance(self.parent_scope_id, str)
        ):
            raise ShadowRequestError("parent_scope_id must be a non-empty string or None")
        if self.scope_id is not None and self.parent_scope_id is not None:
            raise ShadowRequestError("at most one explicit selector may be supplied")


@dataclass(frozen=True, slots=True)
class ProjectInspection:
    project_id: str
    project_name: str
    repository: GitRepository
    git_snapshot: RepositorySnapshot
    work_snapshot: WorkSourceSnapshot
    work_source_kind: str


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    disposition: SelectionDisposition
    scope_id: str | None = None
    item_id: str | None = None
    reason_code: str = ""
    issues: tuple[IntegrationIssue, ...] = ()
    blocking_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreflightAssessment:
    ready: bool
    issues: tuple[IntegrationIssue, ...]
    branch_disposition: BranchDisposition

    @property
    def primary_issue(self) -> IntegrationIssue | None:
        return self.issues[0] if self.issues else None


@dataclass(frozen=True, slots=True)
class ShadowInputs:
    inspection: ProjectInspection
    preflight: PreflightAssessment
    selection: SelectionPlan
    work_package: WorkPackage | None
    input_fingerprint: str


@dataclass(frozen=True, slots=True)
class ShadowReport:
    project_id: str | None
    outcome: ShadowOutcome
    graph_state: GraphState | None
    input_fingerprint: str | None
    head_sha: str | None
    branch: str | None
    work_source_revision: str | None
    selected_scope_id: str | None
    selected_item_id: str | None
    work_package: WorkPackage | None
    branch_disposition: BranchDisposition
    issues: tuple[IntegrationIssue, ...] = ()
    executed_nodes: tuple[str, ...] = ()
    schema_version: int = 1
