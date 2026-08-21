"""Typed, nonce-free CLI result contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SafeCheckpoint:
    checkpoint_id: str
    checkpoint_type: str
    code: str
    message: str
    created_at: str
    expires_at: str
    pending_resume_node: str
    scope_id: str | None = None
    item_id: str | None = None
    repository: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    final_head: str | None = None
    draft: bool | None = None
    pr_title: str | None = None
    publish_plan_digest: str | None = None


@dataclass(frozen=True, slots=True)
class SafePublish:
    repository: str
    base_branch: str
    head_branch: str
    head_sha: str
    pr_number: int
    pr_url: str
    draft: bool
    publish_plan_digest: str


@dataclass(frozen=True, slots=True)
class CliIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class CliErrorView:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class CliResultV1:
    command: str
    ok: bool
    outcome: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    current_node: str | None = None
    pending_resume_node: str | None = None
    scope_id: str | None = None
    scope_branch: str | None = None
    baseline_head: str | None = None
    commit_sha: str | None = None
    completed_item_ids: tuple[str, ...] = ()
    checkpoint: SafeCheckpoint | None = None
    publish: SafePublish | None = None
    issues: tuple[CliIssue, ...] = ()
    profile_digest: str | None = None
    profile_bound: bool | None = None
    profile_match: bool | None = None
    repository: str | None = None
    work_source: str | None = None
    agent_provider: str | None = None
    semantic_review: bool | None = None
    delivery_review: bool | None = None
    publish_description: str | None = None
    decision: str | None = None
    actor: str | None = None
    error: CliErrorView | None = None
    schema_version: int = field(default=1, init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
