"""Immutable authority and evidence contracts for M014 publication."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

from agentgraph.runtime.codec import parse_timestamp, sha256_digest

from .errors import PublishEvidenceError

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.ASCII)


def _validate_digest(value: object, expected: str) -> None:
    if value != expected:
        raise PublishEvidenceError("publish_evidence_digest_mismatch")


def _without(value: object, omitted: str) -> dict[str, object]:
    return {
        field.name: getattr(value, field.name) for field in fields(value) if field.name != omitted
    }


@dataclass(frozen=True, slots=True)
class PublishPlan:
    schema_version: int
    project_id: str
    run_id: str
    scope_id: str
    source_revision: str
    work_plan_digest: str
    target_baseline_head: str
    final_head: str
    final_tree_id: str
    delivery_manifest_digest: str
    delivery_review_evidence_reference: str
    remote_name: str
    remote_host: str
    remote_repository_id: str
    remote_repository_full_name: str
    base_branch: str
    local_scope_branch: str
    remote_head_branch: str
    observed_remote_head_before: str | None
    draft: bool
    pr_title: str
    pr_body: str
    pr_body_digest: str
    operation_id: str
    digest: str

    @classmethod
    def create(cls, **values: object) -> PublishPlan:
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        text = tuple(
            getattr(self, name)
            for name in (
                "project_id",
                "run_id",
                "scope_id",
                "source_revision",
                "work_plan_digest",
                "final_tree_id",
                "delivery_manifest_digest",
                "delivery_review_evidence_reference",
                "remote_name",
                "remote_repository_id",
                "remote_repository_full_name",
                "base_branch",
                "local_scope_branch",
                "remote_head_branch",
            )
        )
        if (
            self.schema_version != 1
            or not self.draft
            or any(not value or "\x00" in value for value in text)
            or self.remote_host != "github.com"
            or self.remote_repository_full_name.count("/") != 1
            or not _SHA.fullmatch(self.target_baseline_head)
            or not _SHA.fullmatch(self.final_head)
            or (
                self.observed_remote_head_before is not None
                and not _SHA.fullmatch(self.observed_remote_head_before)
            )
            or not self.pr_title
            or len(self.pr_title) > 256
            or not self.pr_body
            or len(self.pr_body) > 65536
            or self.pr_body_digest != sha256_digest(self.pr_body)
            or self.operation_id
            != sha256_digest(
                {
                    "project_id": self.project_id,
                    "run_id": self.run_id,
                    "scope_id": self.scope_id,
                    "final_head": self.final_head,
                    "remote_repository_id": self.remote_repository_id,
                    "base_branch": self.base_branch,
                    "remote_head_branch": self.remote_head_branch,
                }
            )
        ):
            raise PublishEvidenceError("publish_plan_invalid")
        _validate_digest(self.digest, sha256_digest(_without(self, "digest")))

    @property
    def marker(self) -> str:
        return f"<!-- agentgraph-publish:{self.operation_id} -->"


@dataclass(frozen=True, slots=True)
class PublishCheckpointRequestRecord:
    schema_version: int
    checkpoint_id: str
    project_id: str
    run_id: str
    code: str
    message: str
    node_id: str
    pending_resume_node: str
    state_version: int
    state_digest: str
    source_revision: str
    work_plan_digest: str
    target_baseline_head: str
    final_head: str
    final_tree_id: str
    delivery_manifest_digest: str
    delivery_review_evidence_reference: str
    publish_plan_digest: str
    operation_id: str
    remote_repository_id: str
    remote_repository_full_name: str
    remote_name: str
    base_branch: str
    remote_head_branch: str
    draft: bool
    pr_title_digest: str
    pr_body_digest: str
    nonce: str
    created_at: str
    expires_at: str
    request_digest: str

    @classmethod
    def create(cls, **values: object) -> PublishCheckpointRequestRecord:
        return cls(**values, request_digest=sha256_digest(values))

    def __post_init__(self) -> None:
        created = parse_timestamp(self.created_at)
        expires = parse_timestamp(self.expires_at)
        text = tuple(
            getattr(self, field.name)
            for field in fields(self)
            if isinstance(getattr(self, field.name), str) and field.name not in {"request_digest"}
        )
        if (
            self.schema_version != 1
            or not self.checkpoint_id.startswith("checkpoint-")
            or self.node_id != "HUMAN_CHECKPOINT"
            or self.pending_resume_node != "CREATE_PR"
            or type(self.state_version) is not int
            or self.state_version < 0
            or not self.draft
            or expires <= created
            or not self.nonce
            or any(not value or "\x00" in value for value in text)
        ):
            raise PublishEvidenceError("publish_checkpoint_request_invalid")
        _validate_digest(self.request_digest, sha256_digest(_without(self, "request_digest")))


@dataclass(frozen=True, slots=True)
class PublishCheckpointView:
    checkpoint_id: str
    nonce: str
    created_at: str
    expires_at: str
    remote_repository_full_name: str
    base_branch: str
    head_branch: str
    final_head: str
    draft: bool
    pr_title: str
    publish_plan_digest: str


@dataclass(frozen=True, slots=True)
class SafeGitCommandReceipt:
    command_id: str
    status: str
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class PushReceipt:
    publish_plan_digest: str
    operation_id: str
    remote_repository_id: str
    remote_repository_full_name: str
    remote_branch: str
    expected_final_head: str
    observed_before_sha: str | None
    observed_after_sha: str
    performed_push: bool
    command_receipt: SafeGitCommandReceipt | None
    digest: str

    @classmethod
    def create(cls, **values: object) -> PushReceipt:
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        if self.observed_after_sha != self.expected_final_head:
            raise PublishEvidenceError("push_receipt_invalid")
        if self.performed_push != (self.command_receipt is not None):
            raise PublishEvidenceError("push_receipt_invalid")
        _validate_digest(self.digest, sha256_digest(_without(self, "digest")))


@dataclass(frozen=True, slots=True)
class PullRequestReceipt:
    publish_plan_digest: str
    operation_id: str
    remote_repository_id: str
    remote_repository_full_name: str
    pr_id: str
    pr_number: int
    pr_url: str
    draft: bool
    head_branch: str
    head_sha: str
    base_branch: str
    adopted_existing: bool
    digest: str

    @classmethod
    def create(cls, **values: object) -> PullRequestReceipt:
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        if not self.draft or self.pr_number < 1:
            raise PublishEvidenceError("pull_request_receipt_invalid")
        _validate_digest(self.digest, sha256_digest(_without(self, "digest")))


@dataclass(frozen=True, slots=True)
class PublishResult:
    project_id: str
    run_id: str
    scope_id: str
    publish_plan_digest: str
    checkpoint_request_digest: str
    checkpoint_decision_digest: str
    final_head: str
    remote_repository_full_name: str
    remote_branch: str
    remote_branch_head: str
    pr_id: str
    pr_number: int
    pr_url: str
    draft: bool
    push_receipt_digest: str
    pr_receipt_digest: str
    digest: str

    @classmethod
    def create(cls, **values: object) -> PublishResult:
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        if not self.draft or self.remote_branch_head != self.final_head:
            raise PublishEvidenceError("publish_result_invalid")
        _validate_digest(self.digest, sha256_digest(_without(self, "digest")))


@dataclass(frozen=True, slots=True)
class PublishReport:
    remote_repository_full_name: str
    base_branch: str
    head_branch: str
    head_sha: str
    pr_number: int
    pr_url: str
    draft: bool
    publish_plan_digest: str
