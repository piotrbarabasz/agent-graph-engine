"""Immutable authority and evidence contracts for M014 publication."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, fields

from agentgraph.core import CheckpointOutcome
from agentgraph.runtime import CheckpointDecision
from agentgraph.runtime.codec import parse_timestamp, sha256_digest

from .errors import PublishEvidenceError

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.ASCII)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_COMMAND_ID = re.compile(r"cmd_[A-Za-z0-9_-]{1,124}", re.ASCII)


def _valid_text(value: object, *, maximum: int = 4096) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value and len(value) <= maximum


def _valid_branch(value: object) -> bool:
    return _valid_text(value, maximum=255) and not str(value).startswith("-")


def _valid_full_name(value: object) -> bool:
    if not _valid_text(value, maximum=201) or str(value).count("/") != 1:
        return False
    owner, repository = str(value).split("/")
    return bool(
        owner and repository and owner.strip() == owner and repository.strip() == repository
    )


def _valid_pr_url(value: object, full_name: str, number: int) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.path == f"/{full_name}/pull/{number}"
        and not parsed.query
        and not parsed.fragment
    )


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
            or not all(
                _DIGEST.fullmatch(value)
                for value in (
                    self.source_revision,
                    self.work_plan_digest,
                    self.delivery_manifest_digest,
                    self.pr_body_digest,
                    self.operation_id,
                    self.digest,
                )
            )
            or self.remote_host != "github.com"
            or not _valid_full_name(self.remote_repository_full_name)
            or not _valid_branch(self.base_branch)
            or not _valid_branch(self.local_scope_branch)
            or not _valid_branch(self.remote_head_branch)
            or not _SHA.fullmatch(self.target_baseline_head)
            or not _SHA.fullmatch(self.final_head)
            or not _SHA.fullmatch(self.final_tree_id)
            or (
                self.observed_remote_head_before is not None
                and not _SHA.fullmatch(self.observed_remote_head_before)
            )
            or not self.pr_title
            or len(self.pr_title) > 256
            or "\x00" in self.pr_title
            or not self.pr_body
            or len(self.pr_body) > 65536
            or "\x00" in self.pr_body
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
            or not all(
                _DIGEST.fullmatch(value)
                for value in (
                    self.state_digest,
                    self.source_revision,
                    self.work_plan_digest,
                    self.delivery_manifest_digest,
                    self.publish_plan_digest,
                    self.operation_id,
                    self.pr_title_digest,
                    self.pr_body_digest,
                    self.request_digest,
                )
            )
            or not _SHA.fullmatch(self.target_baseline_head)
            or not _SHA.fullmatch(self.final_head)
            or not _SHA.fullmatch(self.final_tree_id)
            or not _valid_full_name(self.remote_repository_full_name)
            or not _valid_branch(self.base_branch)
            or not _valid_branch(self.remote_head_branch)
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

    def __post_init__(self) -> None:
        if (
            _COMMAND_ID.fullmatch(self.command_id) is None
            or self.status != "SUCCEEDED"
            or type(self.exit_code) is not int
            or self.exit_code != 0
        ):
            raise PublishEvidenceError("push_command_receipt_invalid")


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
        if (
            not _DIGEST.fullmatch(self.publish_plan_digest)
            or not _DIGEST.fullmatch(self.operation_id)
            or not _valid_text(self.remote_repository_id)
            or not _valid_full_name(self.remote_repository_full_name)
            or not _valid_branch(self.remote_branch)
            or not _SHA.fullmatch(self.expected_final_head)
            or (
                self.observed_before_sha is not None
                and not _SHA.fullmatch(self.observed_before_sha)
            )
            or not _SHA.fullmatch(self.observed_after_sha)
            or type(self.performed_push) is not bool
            or self.observed_after_sha != self.expected_final_head
            or self.observed_before_sha not in {None, self.expected_final_head}
            or self.performed_push != (self.observed_before_sha is None)
        ):
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
        if (
            not _DIGEST.fullmatch(self.publish_plan_digest)
            or not _DIGEST.fullmatch(self.operation_id)
            or not _valid_text(self.remote_repository_id)
            or not _valid_full_name(self.remote_repository_full_name)
            or not _valid_text(self.pr_id, maximum=128)
            or type(self.pr_number) is not int
            or self.pr_number < 1
            or not _valid_pr_url(self.pr_url, self.remote_repository_full_name, self.pr_number)
            or type(self.draft) is not bool
            or not self.draft
            or not _valid_branch(self.head_branch)
            or not _SHA.fullmatch(self.head_sha)
            or not _valid_branch(self.base_branch)
            or type(self.adopted_existing) is not bool
        ):
            raise PublishEvidenceError("pull_request_receipt_invalid")
        _validate_digest(self.digest, sha256_digest(_without(self, "digest")))


@dataclass(frozen=True, slots=True)
class PublishResult:
    project_id: str
    run_id: str
    scope_id: str
    publish_plan_digest: str
    checkpoint_id: str
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
        if (
            not all(_valid_text(value) for value in (self.project_id, self.run_id, self.scope_id))
            or not _DIGEST.fullmatch(self.publish_plan_digest)
            or not self.checkpoint_id.startswith("checkpoint-")
            or not _DIGEST.fullmatch(self.checkpoint_request_digest)
            or not _DIGEST.fullmatch(self.checkpoint_decision_digest)
            or not _SHA.fullmatch(self.final_head)
            or not _valid_full_name(self.remote_repository_full_name)
            or not _valid_branch(self.remote_branch)
            or not _SHA.fullmatch(self.remote_branch_head)
            or not _valid_text(self.pr_id, maximum=128)
            or type(self.pr_number) is not int
            or self.pr_number < 1
            or not _valid_pr_url(self.pr_url, self.remote_repository_full_name, self.pr_number)
            or type(self.draft) is not bool
            or not self.draft
            or not _DIGEST.fullmatch(self.push_receipt_digest)
            or not _DIGEST.fullmatch(self.pr_receipt_digest)
            or self.remote_branch_head != self.final_head
        ):
            raise PublishEvidenceError("publish_result_invalid")
        _validate_digest(self.digest, sha256_digest(_without(self, "digest")))


def verify_publish_evidence_chain(
    plan: PublishPlan,
    request: PublishCheckpointRequestRecord,
    decision: CheckpointDecision,
    push: PushReceipt,
    pull_request: PullRequestReceipt,
    result: PublishResult,
) -> None:
    """Verify the complete durable publication chain without Git or network access."""

    plan_binding = {
        "project_id": plan.project_id,
        "run_id": plan.run_id,
        "source_revision": plan.source_revision,
        "work_plan_digest": plan.work_plan_digest,
        "target_baseline_head": plan.target_baseline_head,
        "final_head": plan.final_head,
        "final_tree_id": plan.final_tree_id,
        "delivery_manifest_digest": plan.delivery_manifest_digest,
        "delivery_review_evidence_reference": plan.delivery_review_evidence_reference,
        "publish_plan_digest": plan.digest,
        "operation_id": plan.operation_id,
        "remote_repository_id": plan.remote_repository_id,
        "remote_repository_full_name": plan.remote_repository_full_name,
        "remote_name": plan.remote_name,
        "base_branch": plan.base_branch,
        "remote_head_branch": plan.remote_head_branch,
        "draft": plan.draft,
        "pr_title_digest": sha256_digest(plan.pr_title),
        "pr_body_digest": plan.pr_body_digest,
    }
    if any(getattr(request, key) != value for key, value in plan_binding.items()):
        raise PublishEvidenceError("publish_checkpoint_binding_mismatch")
    if (
        not _DIGEST.fullmatch(decision.request_digest)
        or not _DIGEST.fullmatch(decision.decision_digest)
        or not _valid_text(decision.nonce)
        or not _valid_text(decision.actor, maximum=256)
        or decision.checkpoint_id != request.checkpoint_id
        or decision.request_digest != request.request_digest
        or decision.nonce != request.nonce
        or decision.outcome is not CheckpointOutcome.APPROVED
        or parse_timestamp(decision.decided_at) < parse_timestamp(request.created_at)
        or parse_timestamp(decision.decided_at) > parse_timestamp(request.expires_at)
    ):
        raise PublishEvidenceError("publish_checkpoint_binding_mismatch")
    if (
        push.publish_plan_digest != plan.digest
        or push.operation_id != plan.operation_id
        or push.remote_repository_id != plan.remote_repository_id
        or push.remote_repository_full_name != plan.remote_repository_full_name
        or push.remote_branch != plan.remote_head_branch
        or push.expected_final_head != plan.final_head
        or push.observed_after_sha != plan.final_head
    ):
        raise PublishEvidenceError("push_receipt_mismatch")
    if (
        pull_request.publish_plan_digest != plan.digest
        or pull_request.operation_id != plan.operation_id
        or pull_request.remote_repository_id != plan.remote_repository_id
        or pull_request.remote_repository_full_name != plan.remote_repository_full_name
        or pull_request.head_branch != plan.remote_head_branch
        or pull_request.head_sha != plan.final_head
        or pull_request.base_branch != plan.base_branch
        or not pull_request.draft
    ):
        raise PublishEvidenceError("pull_request_receipt_mismatch")
    result_binding = {
        "project_id": plan.project_id,
        "run_id": plan.run_id,
        "scope_id": plan.scope_id,
        "publish_plan_digest": plan.digest,
        "checkpoint_id": request.checkpoint_id,
        "checkpoint_request_digest": request.request_digest,
        "checkpoint_decision_digest": decision.decision_digest,
        "final_head": plan.final_head,
        "remote_repository_full_name": plan.remote_repository_full_name,
        "remote_branch": plan.remote_head_branch,
        "remote_branch_head": plan.final_head,
        "pr_id": pull_request.pr_id,
        "pr_number": pull_request.pr_number,
        "pr_url": pull_request.pr_url,
        "draft": True,
        "push_receipt_digest": push.digest,
        "pr_receipt_digest": pull_request.digest,
    }
    if any(getattr(result, key) != value for key, value in result_binding.items()):
        raise PublishEvidenceError("publish_result_mismatch")


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
