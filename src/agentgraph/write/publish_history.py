"""Read-only verification of the durable local publication authority chain."""

from __future__ import annotations

import stat
from pathlib import Path

from agentgraph.agents import (
    AgentAnalysisStatus,
    DeliveryReviewVerdict,
    parse_delivery_review_payload,
)
from agentgraph.core import GraphState, ReviewVerdict, RunStatus
from agentgraph.runtime import CheckpointStore
from agentgraph.runtime.codec import decode_value, parse_json_bytes

from .delivery_storage import verify_delivery_storage
from .errors import PublishEvidenceError
from .evidence import read_evidence
from .models import DeliveryManifest, WriteRunInputs
from .publish_models import (
    PublishCheckpointRequestRecord,
    PublishPlan,
    PublishReport,
    PublishResult,
    PullRequestReceipt,
    PushReceipt,
    verify_publish_evidence_chain,
)
from .publish_storage import load_typed, verify_publish_storage


def publication_expected(state: GraphState) -> bool:
    """Return the M014 terminal-state condition requiring publication evidence."""

    return (
        state.run.status is RunStatus.COMPLETED
        and state.graph.current_node == "END"
        and state.review.verdict is ReviewVerdict.PASS
        and state.review.safe_to_create_pr is True
    )


def read_local_publish_history(
    run_path: Path,
    run_inputs: WriteRunInputs,
    state: GraphState,
    completed_item_ids: tuple[str, ...],
    completed_commit_shas: tuple[str, ...],
) -> PublishReport | None:
    """Verify completed publication using only immutable local evidence."""

    if not publication_expected(state):
        return None

    context = {
        "project_id": run_inputs.project_id,
        "run_id": state.run.run_id,
        "scope_id": run_inputs.scope_id,
    }
    verify_publish_storage(run_path)
    plan = _required_publish(
        load_typed(run_path, "plan.json", PublishPlan, context), "publish_plan_missing"
    )
    push = _required_publish(
        load_typed(run_path, "push.json", PushReceipt, context), "push_receipt_missing"
    )
    pull_request = _required_publish(
        load_typed(run_path, "pull-request.json", PullRequestReceipt, context),
        "pull_request_receipt_missing",
    )
    result = _required_publish(
        load_typed(run_path, "result.json", PublishResult, context), "publish_result_missing"
    )

    request = _publish_checkpoint_request(run_path, plan)
    try:
        decision = CheckpointStore(run_path).load_decision(request.checkpoint_id)
    except Exception as exc:
        raise PublishEvidenceError("publish_checkpoint_decision_invalid") from exc
    if decision is None:
        raise PublishEvidenceError("publish_checkpoint_decision_missing")

    verify_publish_evidence_chain(plan, request, decision, push, pull_request, result)
    manifest = _delivery_manifest(
        run_path,
        run_inputs,
        state.run.run_id,
        completed_item_ids,
        completed_commit_shas,
    )
    _delivery_review(run_path, run_inputs, state, manifest, plan)
    _verify_plan_authority(plan, run_inputs, state.run.run_id, manifest)

    return PublishReport(
        result.remote_repository_full_name,
        plan.base_branch,
        result.remote_branch,
        result.final_head,
        result.pr_number,
        result.pr_url,
        result.draft,
        result.publish_plan_digest,
    )


def verify_publish_checkpoint_plan_binding(
    request: PublishCheckpointRequestRecord, plan: PublishPlan
) -> None:
    """Verify the immutable plan-owned fields of a publish checkpoint request."""

    expected = {
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
        "draft": True,
    }
    if any(getattr(request, name) != value for name, value in expected.items()):
        raise PublishEvidenceError("publish_checkpoint_binding_mismatch")


def _required_publish(value, code: str):
    if value is None:
        raise PublishEvidenceError(code)
    return value


def _publish_checkpoint_request(
    run_path: Path, plan: PublishPlan
) -> PublishCheckpointRequestRecord:
    root = run_path / "checkpoints"
    try:
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise PublishEvidenceError("publish_checkpoint_request_missing") from exc
    except OSError as exc:
        raise PublishEvidenceError("publish_checkpoint_request_invalid") from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise PublishEvidenceError("publish_checkpoint_request_invalid")

    store = CheckpointStore(run_path)
    matches: list[PublishCheckpointRequestRecord] = []
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
        for child in children:
            child_metadata = child.lstat()
            child_attributes = getattr(child_metadata, "st_file_attributes", 0)
            if (
                stat.S_ISLNK(child_metadata.st_mode)
                or bool(child_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
                or not stat.S_ISDIR(child_metadata.st_mode)
            ):
                raise PublishEvidenceError("publish_checkpoint_request_invalid")
            request_path = store.request_path(child.name)
            if not request_path.exists():
                continue
            raw = parse_json_bytes(request_path.read_bytes())
            if not isinstance(raw, dict) or raw.get("pending_resume_node") != "CREATE_PR":
                continue
            request = decode_value(raw, PublishCheckpointRequestRecord)
            if request is not None and request.publish_plan_digest == plan.digest:
                matches.append(request)
    except PublishEvidenceError:
        raise
    except Exception as exc:
        raise PublishEvidenceError("publish_checkpoint_request_invalid") from exc
    if len(matches) != 1:
        code = (
            "publish_checkpoint_request_missing"
            if not matches
            else "publish_checkpoint_request_ambiguous"
        )
        raise PublishEvidenceError(code)
    verify_publish_checkpoint_plan_binding(matches[0], plan)
    return matches[0]


def _delivery_manifest(
    run_path: Path,
    run_inputs: WriteRunInputs,
    run_id: str,
    completed_item_ids: tuple[str, ...],
    completed_commit_shas: tuple[str, ...],
) -> DeliveryManifest:
    path = run_path / "delivery-review" / "manifest.json"
    verify_delivery_storage(run_path, path)
    try:
        document = read_evidence(path)
        manifest = decode_value(document.get("payload"), DeliveryManifest)
    except Exception as exc:
        raise PublishEvidenceError("delivery_manifest_invalid") from exc
    authority = {
        "project_id": run_inputs.project_id,
        "run_id": run_id,
        "scope_id": run_inputs.scope_id,
        "source_revision": run_inputs.source_revision,
        "work_plan_digest": run_inputs.work_plan_digest,
        "target_baseline_head": run_inputs.target_baseline_head,
    }
    if (
        any(document.get(name) != value for name, value in authority.items())
        or manifest.scope_id != run_inputs.scope_id
        or manifest.source_revision != run_inputs.source_revision
        or manifest.work_plan_digest != run_inputs.work_plan_digest
        or manifest.target_baseline_head != run_inputs.target_baseline_head
        or manifest.completed_item_ids != completed_item_ids
        or manifest.completed_commit_shas != completed_commit_shas
        or not completed_commit_shas
        or manifest.final_head != completed_commit_shas[-1]
    ):
        raise PublishEvidenceError("delivery_manifest_mismatch")
    return manifest


def _delivery_review(
    run_path: Path,
    run_inputs: WriteRunInputs,
    state: GraphState,
    manifest: DeliveryManifest,
    plan: PublishPlan,
) -> None:
    path = run_path / "delivery-review" / "review.json"
    verify_delivery_storage(run_path, path)
    try:
        document = read_evidence(path)
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid review payload")
        analysis = parse_delivery_review_payload(payload.get("analysis"))
    except Exception as exc:
        raise PublishEvidenceError("delivery_review_evidence_invalid") from exc
    authority = {
        "project_id": run_inputs.project_id,
        "run_id": state.run.run_id,
        "scope_id": run_inputs.scope_id,
        "source_revision": run_inputs.source_revision,
        "work_plan_digest": run_inputs.work_plan_digest,
        "target_baseline_head": run_inputs.target_baseline_head,
        "final_head": manifest.final_head,
        "final_tree_id": manifest.final_tree_id,
        "delivery_manifest_digest": manifest.digest,
    }
    if (
        any(document.get(name) != value for name, value in authority.items())
        or plan.delivery_review_evidence_reference != "delivery-review/review.json"
        or analysis.status is not AgentAnalysisStatus.SUCCESS
        or analysis.verdict is not DeliveryReviewVerdict.PASS
        or state.review.verdict is not ReviewVerdict.PASS
        or state.review.safe_to_create_pr is not True
    ):
        raise PublishEvidenceError("delivery_review_evidence_mismatch")


def _verify_plan_authority(
    plan: PublishPlan,
    run_inputs: WriteRunInputs,
    run_id: str,
    manifest: DeliveryManifest,
) -> None:
    if (
        plan.project_id != run_inputs.project_id
        or plan.run_id != run_id
        or plan.scope_id != run_inputs.scope_id
        or plan.source_revision != run_inputs.source_revision
        or plan.work_plan_digest != run_inputs.work_plan_digest
        or plan.target_baseline_head != run_inputs.target_baseline_head
        or plan.base_branch != run_inputs.base_branch
        or plan.local_scope_branch != run_inputs.scope_branch
        or plan.remote_head_branch != run_inputs.scope_branch
        or plan.delivery_manifest_digest != manifest.digest
        or plan.final_head != manifest.final_head
        or plan.final_tree_id != manifest.final_tree_id
    ):
        raise PublishEvidenceError("publish_evidence_mismatch")
