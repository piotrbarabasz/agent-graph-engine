"""Immutable public contracts for one controlled local write slice."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from agentgraph.core import GraphState
from agentgraph.work import RepoPathSpec, ValidationCheck, WorkPackage

from .errors import ChangeSetError

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


class WriteSliceOutcome(StrEnum):
    LOCAL_COMMIT_CREATED = "local_commit_created"
    BLOCKED = "blocked"
    FAILED = "failed"
    INVALID_SOURCE = "invalid_source"
    RECOVERY_REQUIRED = "recovery_required"


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
