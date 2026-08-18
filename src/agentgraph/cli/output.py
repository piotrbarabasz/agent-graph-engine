"""Stable JSON and compact human rendering."""

from __future__ import annotations

import json
from dataclasses import replace

from agentgraph.write import (
    CheckpointView,
    PublishCheckpointView,
    WriteSliceOutcome,
    WriteSliceReport,
)

from .models import CliIssue, CliResultV1, SafeCheckpoint, SafePublish
from .status import StatusReport


def from_write_report(command: str, report: WriteSliceReport) -> CliResultV1:
    state = report.graph_state
    checkpoint = _checkpoint_from_write(report)
    publish = (
        None
        if report.publish is None
        else SafePublish(
            report.publish.remote_repository_full_name,
            report.publish.base_branch,
            report.publish.head_branch,
            report.publish.head_sha,
            report.publish.pr_number,
            report.publish.pr_url,
            report.publish.draft,
            report.publish.publish_plan_digest,
        )
    )
    ok = report.outcome not in {
        WriteSliceOutcome.BLOCKED,
        WriteSliceOutcome.INVALID_SOURCE,
        WriteSliceOutcome.RECOVERY_REQUIRED,
        WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED,
        WriteSliceOutcome.FAILED,
    }
    return CliResultV1(
        command=command,
        ok=ok,
        outcome=report.outcome.name,
        project_id=report.project_id,
        run_id=report.run_id,
        run_status=None if state is None else state.run.status.value,
        current_node=None if state is None else state.graph.current_node,
        pending_resume_node=None if state is None else state.graph.pending_resume_node,
        scope_id=report.scope_id,
        scope_branch=report.scope_branch,
        baseline_head=report.baseline_head,
        commit_sha=report.commit_sha,
        completed_item_ids=report.completed_item_ids,
        checkpoint=checkpoint,
        publish=publish,
        issues=tuple(CliIssue(issue.code, issue.message) for issue in report.issues),
    )


def from_status(command: str, report: StatusReport) -> CliResultV1:
    mismatch = report.profile_match is False
    issues = report.issues + (
        (CliIssue("execution_profile_mismatch", "current execution profile differs"),)
        if mismatch
        else ()
    )
    return CliResultV1(
        command=command,
        ok=not mismatch,
        outcome="RECOVERY_REQUIRED" if mismatch else "STATUS",
        project_id=report.project_id,
        run_id=report.run_id,
        run_status=report.run_status,
        current_node=report.current_node,
        pending_resume_node=report.pending_resume_node,
        scope_id=report.scope_id,
        scope_branch=report.scope_branch,
        baseline_head=report.baseline_head,
        commit_sha=report.commit_sha,
        completed_item_ids=report.completed_item_ids,
        checkpoint=report.checkpoint,
        publish=report.publish,
        issues=issues,
        profile_digest=report.profile_digest,
        profile_bound=report.profile_bound,
        profile_match=report.profile_match,
    )


def render_json(result: CliResultV1) -> str:
    return json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def render_human(result: CliResultV1) -> str:
    if result.error is not None:
        location = f"\nFile: {result.error.path}" if result.error.path else ""
        return f"Error [{result.error.code}]: {result.error.message}{location}"
    if result.command == "config validate":
        return "\n".join(
            (
                "Configuration valid",
                f"Repository: {result.repository}",
                f"Work source: {result.work_source}",
                f"Agent provider: {result.agent_provider}",
                f"Semantic review: {_enabled(result.semantic_review)}",
                f"Delivery review: {_enabled(result.delivery_review)}",
                f"Publish: {result.publish_description}",
                f"Profile digest: {result.profile_digest}",
            )
        )
    if result.command.startswith("checkpoint ") and result.decision is not None:
        label = {
            "APPROVED": "Approval",
            "REJECTED": "Rejection",
            "CANCELLED": "Cancellation",
        }[result.decision]
        return (
            f"{label} recorded.\nRun ID: {result.run_id}\nNext:\nagentgraph resume {result.run_id}"
        )
    lines = []
    if result.outcome:
        lines.append(f"Outcome: {result.outcome}")
    for label, value in (
        ("Run ID", result.run_id),
        ("Scope", result.scope_id),
        ("Run status", result.run_status),
        ("Current node", result.current_node),
        ("Completed items", ", ".join(result.completed_item_ids) or None),
        ("Commit/head", result.commit_sha),
        ("Profile digest", result.profile_digest),
        ("Profile match", result.profile_match),
    ):
        if value is not None:
            lines.append(f"{label}: {value}")
    if result.checkpoint is not None:
        lines.extend(_checkpoint_lines(result.checkpoint))
    if result.publish is not None:
        lines.append(f"Draft PR: {result.publish.pr_url}")
    for issue in result.issues:
        lines.append(f"Issue [{issue.code}]: {issue.message}")
    lines.extend(_next_action(result))
    return "\n".join(lines)


def with_profile(result: CliResultV1, digest: str) -> CliResultV1:
    mismatch = any(issue.code == "execution_profile_mismatch" for issue in result.issues)
    return replace(
        result,
        profile_digest=digest,
        profile_bound=True,
        profile_match=not mismatch,
    )


def _checkpoint_from_write(report: WriteSliceReport) -> SafeCheckpoint | None:
    checkpoint = report.checkpoint
    if isinstance(checkpoint, PublishCheckpointView):
        return SafeCheckpoint(
            checkpoint.checkpoint_id,
            "publish",
            "publish_approval_required",
            "publication requires explicit human approval",
            checkpoint.created_at,
            checkpoint.expires_at,
            "CREATE_PR",
            scope_id=report.scope_id,
            repository=checkpoint.remote_repository_full_name,
            base_branch=checkpoint.base_branch,
            head_branch=checkpoint.head_branch,
            final_head=checkpoint.final_head,
            draft=checkpoint.draft,
            pr_title=checkpoint.pr_title,
            publish_plan_digest=checkpoint.publish_plan_digest,
        )
    if isinstance(checkpoint, CheckpointView):
        return SafeCheckpoint(
            checkpoint.checkpoint_id,
            "item_risk",
            checkpoint.code,
            checkpoint.message,
            checkpoint.created_at,
            checkpoint.expires_at,
            checkpoint.pending_resume_node,
            scope_id=report.scope_id,
            item_id=report.item_id,
        )
    return None


def _checkpoint_lines(checkpoint: SafeCheckpoint) -> list[str]:
    lines = [
        f"Checkpoint: {checkpoint.checkpoint_type}",
        f"Checkpoint ID: {checkpoint.checkpoint_id}",
        f"Code: {checkpoint.code}",
        f"Message: {checkpoint.message}",
        f"Created: {checkpoint.created_at}",
        f"Expires: {checkpoint.expires_at}",
        f"Pending operation: {checkpoint.pending_resume_node}",
    ]
    for label, value in (
        ("Repository", checkpoint.repository),
        ("Base branch", checkpoint.base_branch),
        ("Head branch", checkpoint.head_branch),
        ("Final SHA", checkpoint.final_head),
        ("Draft", checkpoint.draft),
        ("PR title", checkpoint.pr_title),
        ("Publish plan", checkpoint.publish_plan_digest),
    ):
        if value is not None:
            lines.append(f"{label}: {value}")
    return lines


def _next_action(result: CliResultV1) -> list[str]:
    if result.run_id is None:
        return []
    if result.outcome in {"CHECKPOINT_REQUIRED", "PUBLISH_CHECKPOINT_REQUIRED"}:
        return ["Next:", f"agentgraph checkpoint show {result.run_id}"]
    if result.outcome == "PUBLISH_PREPARATION_BLOCKED":
        return ["Fix the remote/auth issue and run:", f"agentgraph resume {result.run_id}"]
    return []


def _enabled(value: bool | None) -> str:
    return "enabled" if value else "disabled"
