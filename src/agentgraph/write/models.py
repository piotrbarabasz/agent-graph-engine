"""Immutable public contracts for one controlled local write slice."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from agentgraph.core import (
    FailureCategory,
    GraphState,
    RepairClassification,
    ReviewVerdict,
    RiskLevel,
)
from agentgraph.runtime.codec import sha256_digest
from agentgraph.work import RepoPathSpec, ValidationCheck, WorkPackage

from .errors import ChangeSetError
from .publish_models import PublishCheckpointView, PublishReport

MAX_CHANGE_FILES = 20
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    expected_before_sha256: str | None
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or "\x00" in self.path:
            raise ChangeSetError("change path must be a non-empty NUL-free string")
        if self.expected_before_sha256 is not None and not _is_sha256(self.expected_before_sha256):
            raise ChangeSetError("expected_before_sha256 must be a lowercase SHA-256 hex digest")
        if not isinstance(self.content, str) or "\x00" in self.content:
            raise ChangeSetError("file content must be NUL-free text")
        if len(_utf8(self.content)) > MAX_FILE_BYTES:
            raise ChangeSetError("file content exceeds the M006 per-file limit")


@dataclass(frozen=True, slots=True)
class ChangeSet:
    changes: tuple[FileChange, ...]
    digest: str

    def __post_init__(self) -> None:
        if not self.changes or len(self.changes) > MAX_CHANGE_FILES:
            raise ChangeSetError("changeset must contain between 1 and 20 files")
        if not all(isinstance(change, FileChange) for change in self.changes):
            raise ChangeSetError("changeset contains an invalid file change")
        if len({change.path for change in self.changes}) != len(self.changes):
            raise ChangeSetError("changeset contains duplicate paths")
        if sum(len(_utf8(change.content)) for change in self.changes) > MAX_TOTAL_BYTES:
            raise ChangeSetError("changeset exceeds the M006 total-size limit")
        expected = changeset_digest(self.changes)
        if self.digest != expected:
            raise ChangeSetError("changeset digest does not match canonical content")

    @classmethod
    def create(cls, changes: tuple[FileChange, ...]) -> ChangeSet:
        return cls(changes, changeset_digest(changes))


class ChangeIntent(StrEnum):
    IMPLEMENT = "implement"
    PROGRAMMER_REPAIR = "programmer_repair"
    DEBUGGER = "debugger"


class RepairValidationDiagnosticKind(StrEnum):
    DECLARED_VALIDATION = "declared_validation"
    GIT_DIFF_CHECK_WORKTREE = "git_diff_check_worktree"
    GIT_DIFF_CHECK_STAGED = "git_diff_check_staged"


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    project_id: str
    item_id: str
    scope_id: str
    title: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    test_requirements: tuple[str, ...]
    allowed_paths: tuple[RepoPathSpec, ...]
    source_revision: str
    baseline_head: str
    architecture_invariants: tuple[str, ...]
    analysis_summary: tuple[str, ...] = ()
    implementation_plan: tuple[str, ...] = ()
    validation_focus: tuple[str, ...] = ()
    derived_constraints: tuple[str, ...] = ()
    relevant_files: tuple[str, ...] = ()
    effective_requirements: tuple[str, ...] = ()
    effective_acceptance_criteria: tuple[str, ...] = ()
    intent: ChangeIntent = ChangeIntent.IMPLEMENT
    repair_cycle: int = 0
    failure_category: FailureCategory | None = None
    failure_code: str | None = None
    failure_source: str | None = None
    validation_diagnostics: tuple[str, ...] = ()
    review_findings: tuple[str, ...] = ()
    current_changed_paths: tuple[str, ...] = ()
    current_manifest_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, ChangeIntent):
            raise ChangeSetError("change intent must be typed")
        if type(self.repair_cycle) is not int or self.repair_cycle < 0:
            raise ChangeSetError("repair cycle must be non-negative")


@dataclass(frozen=True, slots=True)
class AppliedFile:
    path: str
    before_sha256: str | None
    after_sha256: str
    size_bytes: int
    before_mode: int | None
    after_mode: int


@dataclass(frozen=True, slots=True)
class AppliedChangeSet:
    files: tuple[AppliedFile, ...]
    changeset_digest: str


@dataclass(frozen=True, slots=True)
class CommitWitness:
    project_id: str
    run_id: str
    item_id: str
    scope_id: str
    base_head: str
    previous_branch_head: str
    commit_sha: str | None
    changeset_digest: str
    reviewed_paths: tuple[str, ...]
    workspace_manifest_digest: str | None = None
    repair_count: int = 0


@dataclass(frozen=True, slots=True)
class WorkspaceManifestEntry:
    path: str
    sha256: str
    size_bytes: int
    mode: int

    def __post_init__(self) -> None:
        if (
            not self.path
            or self.mode not in {0o644, 0o755}
            or self.size_bytes < 0
            or len(self.sha256) != 64
        ):
            raise ChangeSetError("workspace manifest entry is invalid")


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    cycle: int
    baseline_head: str
    files: tuple[WorkspaceManifestEntry, ...]
    digest: str

    @classmethod
    def create(
        cls,
        cycle: int,
        baseline_head: str,
        files: tuple[WorkspaceManifestEntry, ...],
    ) -> WorkspaceManifest:
        payload = {
            "cycle": cycle,
            "baseline_head": baseline_head,
            "files": files,
        }
        return cls(cycle, baseline_head, files, sha256_digest(payload))

    def __post_init__(self) -> None:
        if self.cycle < 0 or self.digest != sha256_digest(
            {
                "cycle": self.cycle,
                "baseline_head": self.baseline_head,
                "files": self.files,
            }
        ):
            raise ChangeSetError("workspace manifest digest is invalid")


@dataclass(frozen=True, slots=True)
class RepairFailureContext:
    cycle: int
    failure_source_node: str
    failure_category: FailureCategory
    failure_code: str
    current_manifest_digest: str
    current_changed_paths: tuple[str, ...]
    validation_diagnostics: tuple[RepairValidationDiagnostic, ...]
    review_findings: tuple[str, ...]
    effective_requirements: tuple[str, ...]
    effective_acceptance_criteria: tuple[str, ...]
    allowed_paths: tuple[RepoPathSpec, ...]
    baseline_head: str
    source_revision: str
    classification: RepairClassification | None = None


@dataclass(frozen=True, slots=True)
class RepairValidationDiagnostic:
    kind: RepairValidationDiagnosticKind
    command_id: str
    status: str
    exit_code: int | None
    stdout_preview: str
    stderr_preview: str
    stdout_truncated: bool
    stderr_truncated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RepairValidationDiagnosticKind):
            raise ChangeSetError("repair validation diagnostic kind must be typed")


@dataclass(frozen=True, slots=True)
class SemanticReviewContext:
    cycle: int
    item_id: str
    scope_id: str
    goal: str
    current_manifest_digest: str
    current_changed_paths: tuple[str, ...]
    effective_requirements: tuple[str, ...]
    effective_acceptance_criteria: tuple[str, ...]
    architecture_invariants: tuple[str, ...]
    derived_constraints: tuple[str, ...]
    validation_diagnostics: tuple[RepairValidationDiagnostic, ...]
    allowed_paths: tuple[RepoPathSpec, ...]
    baseline_head: str
    source_revision: str
    risk_level: RiskLevel
    relevant_files: tuple[str, ...]
    digest: str

    @classmethod
    def create(cls, **values) -> SemanticReviewContext:
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        values = {
            field: getattr(self, field) for field in self.__dataclass_fields__ if field != "digest"
        }
        if self.digest != sha256_digest(values):
            raise ChangeSetError("semantic review context digest is invalid")


@dataclass(frozen=True, slots=True)
class WriteInputs:
    project_id: str
    package: WorkPackage
    expected_allowed_paths: tuple[RepoPathSpec, ...]
    source_revision: str
    baseline_head: str
    base_branch: str
    scope_branch: str
    item_validation_checks: tuple[ValidationCheck, ...]
    scope_required_checks: tuple[ValidationCheck, ...]
    capability_fingerprint: str
    max_repair_cycles: int = 0
    semantic_review_enabled: bool = False
    checkpoint_ttl_seconds: int = 3600
    target_baseline_head: str | None = None
    target_base_branch: str | None = None
    item_index: int = 1
    work_plan_digest: str = ""
    run_inputs_digest: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.checkpoint_ttl_seconds) is not int
            or not 60 <= self.checkpoint_ttl_seconds <= 86400
        ):
            raise ValueError("checkpoint TTL must be between 60 and 86400 seconds")
        if type(self.item_index) is not int or self.item_index < 1:
            raise ValueError("item index must be positive")

    @property
    def pinned_target_head(self) -> str:
        return self.target_baseline_head or self.baseline_head

    @property
    def pinned_target_branch(self) -> str:
        return self.target_base_branch or self.base_branch


@dataclass(frozen=True, slots=True)
class WriteRunInputs:
    schema_version: int
    project_id: str
    scope_id: str
    parent_scope_id: str | None
    source_revision: str
    target_baseline_head: str
    base_branch: str
    scope_branch: str
    work_plan_digest: str
    max_work_items_per_run: int
    max_repair_cycles: int
    semantic_review_enabled: bool
    checkpoint_ttl_seconds: int
    delivery_review_enabled: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported write-run inputs schema")
        if type(self.delivery_review_enabled) is not bool:
            raise ValueError("delivery review mode must be boolean")
        if not 1 <= self.max_work_items_per_run <= 20:
            raise ValueError("work-item limit must be between 1 and 20")
        if self.max_repair_cycles not in {0, 1, 2}:
            raise ValueError("repair limit is invalid")
        if not 60 <= self.checkpoint_ttl_seconds <= 86400:
            raise ValueError("checkpoint TTL is invalid")
        if not all(
            (
                self.project_id,
                self.scope_id,
                self.source_revision,
                self.target_baseline_head,
                self.base_branch,
                self.scope_branch,
                self.work_plan_digest,
            )
        ):
            raise ValueError("write-run inputs contain an empty authority field")


def write_run_inputs_digest(run: WriteRunInputs) -> str:
    """Digest run authority without changing the final-M012 disabled-mode identity."""

    authority = {
        "schema_version": run.schema_version,
        "project_id": run.project_id,
        "scope_id": run.scope_id,
        "parent_scope_id": run.parent_scope_id,
        "source_revision": run.source_revision,
        "target_baseline_head": run.target_baseline_head,
        "base_branch": run.base_branch,
        "scope_branch": run.scope_branch,
        "work_plan_digest": run.work_plan_digest,
        "max_work_items_per_run": run.max_work_items_per_run,
        "max_repair_cycles": run.max_repair_cycles,
        "semantic_review_enabled": run.semantic_review_enabled,
        "checkpoint_ttl_seconds": run.checkpoint_ttl_seconds,
    }
    if run.delivery_review_enabled:
        authority["delivery_review_enabled"] = True
    return sha256_digest(authority)


@dataclass(frozen=True, slots=True)
class WorkPlanItem:
    plan_index: int
    item_id: str
    package: WorkPackage
    package_digest: str
    allowed_paths: tuple[RepoPathSpec, ...]
    capability_fingerprint: str

    def __post_init__(self) -> None:
        capability = [
            {"path": item.path, "directory_hint": item.directory_hint}
            for item in self.allowed_paths
        ]
        raw = json.dumps(capability, sort_keys=True, separators=(",", ":")).encode()
        expected_capability = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if (
            self.plan_index < 1
            or self.package.item_id != self.item_id
            or self.package_digest != sha256_digest(self.package)
            or self.capability_fingerprint != expected_capability
        ):
            raise ValueError("work plan item binding is invalid")


@dataclass(frozen=True, slots=True)
class WorkPlan:
    schema_version: int
    scope_id: str
    source_revision: str
    items: tuple[WorkPlanItem, ...]
    digest: str

    @classmethod
    def create(
        cls, scope_id: str, source_revision: str, items: tuple[WorkPlanItem, ...]
    ) -> WorkPlan:
        values = {
            "schema_version": 1,
            "scope_id": scope_id,
            "source_revision": source_revision,
            "items": items,
        }
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        values = {
            "schema_version": self.schema_version,
            "scope_id": self.scope_id,
            "source_revision": self.source_revision,
            "items": self.items,
        }
        if self.schema_version != 1 or not self.items or self.digest != sha256_digest(values):
            raise ValueError("work plan digest is invalid")
        if tuple(item.plan_index for item in self.items) != tuple(range(1, len(self.items) + 1)):
            raise ValueError("work plan indexes are invalid")
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("work plan contains duplicate items")
        if any(
            item.package.scope_id != self.scope_id
            or item.package.source_revision.fingerprint != self.source_revision
            for item in self.items
        ):
            raise ValueError("work plan item authority is inconsistent")


class WriteSliceOutcome(StrEnum):
    LOCAL_COMMIT_CREATED = "local_commit_created"
    BLOCKED = "blocked"
    FAILED = "failed"
    INVALID_SOURCE = "invalid_source"
    RECOVERY_REQUIRED = "recovery_required"
    CHECKPOINT_REQUIRED = "checkpoint_required"
    DELIVERY_REVIEW_REQUIRED = "delivery_review_required"
    DELIVERY_CHECKPOINT_REQUIRED = "delivery_checkpoint_required"
    PUBLISH_PREPARATION_BLOCKED = "publish_preparation_blocked"
    PUBLISH_CHECKPOINT_REQUIRED = "publish_checkpoint_required"
    DRAFT_PR_CREATED = "draft_pr_created"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class CheckpointView:
    checkpoint_id: str
    code: str
    message: str
    nonce: str
    created_at: str
    expires_at: str
    pending_resume_node: str


@dataclass(frozen=True, slots=True)
class WriteSliceRequest:
    scope_id: str | None = None
    parent_scope_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.scope_id, self.parent_scope_id):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("write selector must be a non-empty string or None")
        if self.scope_id is not None and self.parent_scope_id is not None:
            raise ValueError("at most one write selector may be supplied")


@dataclass(frozen=True, slots=True)
class WriteSliceIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class CompletedItemReport:
    item_id: str
    item_index: int
    item_base_head: str
    commit_sha: str
    changeset_digest: str
    workspace_manifest_digest: str
    changed_paths: tuple[str, ...]
    repair_count: int


@dataclass(frozen=True, slots=True)
class DeliveryManifestEntry:
    path: str
    sha256: str
    size_bytes: int
    mode: int
    object_id: str

    def __post_init__(self) -> None:
        if (
            not self.path
            or self.mode not in {0o644, 0o755}
            or self.size_bytes < 0
            or not _is_sha256(self.sha256)
            or not self.object_id
        ):
            raise ChangeSetError("delivery manifest entry is invalid")


@dataclass(frozen=True, slots=True)
class DeliveryManifest:
    schema_version: int
    scope_id: str
    target_baseline_head: str
    final_head: str
    final_tree_id: str
    changed_files: tuple[DeliveryManifestEntry, ...]
    completed_item_ids: tuple[str, ...]
    completed_commit_shas: tuple[str, ...]
    work_plan_digest: str
    source_revision: str
    digest: str

    @classmethod
    def create(cls, **values) -> DeliveryManifest:
        values = {"schema_version": 1, **values}
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        values = {
            field: getattr(self, field) for field in self.__dataclass_fields__ if field != "digest"
        }
        if (
            self.schema_version != 1
            or self.digest != sha256_digest(values)
            or len({item.path for item in self.changed_files}) != len(self.changed_files)
            or len(self.completed_item_ids) != len(self.completed_commit_shas)
        ):
            raise ChangeSetError("delivery manifest is invalid")


@dataclass(frozen=True, slots=True)
class DeliveryCompletedItem:
    item_id: str
    item_index: int
    title: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    test_requirements: tuple[str, ...]
    item_base_head: str
    commit_sha: str
    changed_paths: tuple[str, ...]
    repair_count: int


@dataclass(frozen=True, slots=True)
class DeliveryReviewContext:
    schema_version: int
    scope_id: str
    source_revision: str
    work_plan_digest: str
    target_baseline_head: str
    final_head: str
    final_tree_id: str
    delivery_manifest_digest: str
    final_changed_paths: tuple[str, ...]
    delivery_allowed_paths: tuple[RepoPathSpec, ...]
    completed_items: tuple[DeliveryCompletedItem, ...]
    declared_work: tuple[DeliveryCompletedItem, ...]
    architecture_invariants: tuple[str, ...]
    context_digest: str

    @classmethod
    def create(cls, **values) -> DeliveryReviewContext:
        values = {"schema_version": 1, **values}
        return cls(**values, context_digest=sha256_digest(values))

    def __post_init__(self) -> None:
        values = {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
            if field != "context_digest"
        }
        if self.schema_version != 1 or self.context_digest != sha256_digest(values):
            raise ChangeSetError("delivery review context is invalid")


@dataclass(frozen=True, slots=True)
class DeliveryReviewReport:
    scope_id: str
    target_baseline_head: str
    final_head: str
    final_tree_id: str
    manifest_digest: str
    verdict: ReviewVerdict
    safe_to_create_pr: bool
    findings: tuple[str, ...]
    evidence_reference: str


@dataclass(frozen=True, slots=True)
class WriteSliceReport:
    outcome: WriteSliceOutcome
    project_id: str | None
    run_id: str | None
    graph_state: GraphState | None
    selected_item_id: str | None
    selected_scope_id: str | None
    source_revision: str | None
    baseline_head: str | None
    scope_branch: str | None
    commit_sha: str | None
    runtime_path: str | None
    workspace_path: str | None
    executed_nodes: tuple[str, ...] = ()
    issues: tuple[WriteSliceIssue, ...] = ()
    changeset_digest: str | None = None
    changed_paths: tuple[str, ...] = ()
    checkpoint: CheckpointView | PublishCheckpointView | None = None
    completed_item_ids: tuple[str, ...] = ()
    commit_shas: tuple[str, ...] = ()
    completed_items: tuple[CompletedItemReport, ...] = ()
    delivery_review: DeliveryReviewReport | None = None
    publish: PublishReport | None = None

    @property
    def final_graph_state(self) -> GraphState | None:
        return self.graph_state

    @property
    def item_id(self) -> str | None:
        return self.selected_item_id

    @property
    def scope_id(self) -> str | None:
        return self.selected_scope_id

    @property
    def base_head(self) -> str | None:
        return self.baseline_head

    @property
    def runtime_reference(self) -> str | None:
        if self.project_id is None or self.run_id is None:
            return None
        return f"{self.project_id}/{self.run_id}"

    @property
    def work_source_revision(self) -> str | None:
        return self.source_revision


def changeset_digest(changes: tuple[FileChange, ...]) -> str:
    payload = [
        {
            "path": change.path,
            "expected_before_sha256": change.expected_before_sha256,
            "content_sha256": hashlib.sha256(_utf8(change.content)).hexdigest(),
        }
        for change in sorted(changes, key=lambda value: value.path.encode("utf-8"))
    ]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ChangeSetError("file content must be valid UTF-8 text") from exc
