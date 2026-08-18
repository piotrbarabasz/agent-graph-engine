"""Exact, approval-bound Git push and draft pull-request orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from agentgraph.core import (
    CheckpointOutcome,
    CheckpointRequest,
    Evidence,
    ExternalEffect,
    FailureCategory,
    GraphState,
    NodeContext,
    NodeResult,
    NodeStatus,
    ResultReason,
    ReviewVerdict,
)
from agentgraph.infra.errors import GitError
from agentgraph.infra.git import GitRemoteEndpoint
from agentgraph.runtime import (
    CheckpointDecision,
    CheckpointStore,
    InterruptedSideEffectAssessment,
)
from agentgraph.runtime.codec import (
    decode_value,
    format_timestamp,
    parse_timestamp,
    sha256_digest,
    utc_now,
)
from agentgraph.runtime.errors import CheckpointEvidenceError, CheckpointStoreError
from agentgraph.runtime.state_store import StateStore

from .errors import (
    CheckpointBindingError,
    CheckpointError,
    PublishConflictError,
    PublishEvidenceError,
    PublishPreparationError,
    WorkspaceError,
)
from .evidence import read_evidence
from .models import DeliveryManifest
from .publish_models import (
    PublishCheckpointRequestRecord,
    PublishCheckpointView,
    PublishPlan,
    PublishReport,
    PublishResult,
    PullRequestReceipt,
    PushReceipt,
    SafeGitCommandReceipt,
    verify_publish_evidence_chain,
)
from .publish_storage import load_typed, persist_once
from .remote import (
    DraftPullRequestRequest,
    RemoteContractError,
    RemoteProvider,
    RemoteProviderError,
    RemoteRepositoryIdentity,
    RemoteServiceError,
    UnsafeRemoteUrlError,
    UnsupportedRemoteHostError,
    parse_github_remote_url,
)

PUBLISH_CHECKPOINT_CODE = "publish_approval_required"
PUBLISH_CHECKPOINT_MESSAGE = "Exact draft pull-request publication requires approval."


@dataclass(slots=True)
class PublishExecution:
    controller: object
    provider: RemoteProvider | None
    remote_name: str = "origin"
    clock: Callable[[], datetime] = utc_now
    nonce_factory: Callable[[], str] | None = None
    plan: PublishPlan | None = field(default=None, init=False)
    result: PublishResult | None = field(default=None, init=False)
    issue_code: str | None = field(default=None, init=False)

    @property
    def run_path(self):
        return self.controller.run_path

    @property
    def context(self) -> dict[str, str]:
        run = self.controller.run_inputs
        return {
            "project_id": run.project_id,
            "run_id": self.controller.run_id,
            "scope_id": run.scope_id,
        }

    def ensure_plan(self, state: GraphState) -> PublishPlan:
        self._require_publish_checkpoint(state)
        manifest, review, completed = self._delivery_authority(state)
        workspace = self._verify_local(manifest.final_head)
        endpoint, identity = self._push_endpoint(workspace)

        existing = load_typed(self.run_path, "plan.json", PublishPlan, self.context)
        observed = self.controller.git.remote_branch_sha_at_endpoint(
            workspace, endpoint, self.controller.run_inputs.scope_branch
        )
        if observed not in {None, manifest.final_head}:
            raise PublishConflictError("remote_branch_conflict")
        operation_id = sha256_digest(
            {
                "project_id": self.controller.run_inputs.project_id,
                "run_id": self.controller.run_id,
                "scope_id": self.controller.run_inputs.scope_id,
                "final_head": manifest.final_head,
                "remote_repository_id": identity.repository_id,
                "base_branch": self.controller.run_inputs.base_branch,
                "remote_head_branch": self.controller.run_inputs.scope_branch,
            }
        )
        title = f"AgentGraph: {self.controller.run_inputs.scope_id}"
        marker = f"<!-- agentgraph-publish:{operation_id} -->"
        item_lines = "\n".join(f"- {item.item_id}: `{item.commit_sha}`" for item in completed)
        body = (
            f"{marker}\n\n"
            f"Scope: `{self.controller.run_inputs.scope_id}`\n\n"
            f"Completed items:\n{item_lines}\n\n"
            f"Target baseline: `{self.controller.run_inputs.target_baseline_head}`\n\n"
            f"Final head: `{manifest.final_head}`\n\n"
            "Delivery review: PASS\n\nDraft: true\n"
        )
        values = {
            "schema_version": 1,
            "project_id": self.controller.run_inputs.project_id,
            "run_id": self.controller.run_id,
            "scope_id": self.controller.run_inputs.scope_id,
            "source_revision": self.controller.run_inputs.source_revision,
            "work_plan_digest": self.controller.run_inputs.work_plan_digest,
            "target_baseline_head": self.controller.run_inputs.target_baseline_head,
            "final_head": manifest.final_head,
            "final_tree_id": manifest.final_tree_id,
            "delivery_manifest_digest": manifest.digest,
            "delivery_review_evidence_reference": review.evidence_reference,
            "remote_name": self.remote_name,
            "remote_host": identity.host,
            "remote_repository_id": identity.repository_id,
            "remote_repository_full_name": identity.full_name,
            "base_branch": self.controller.run_inputs.base_branch,
            "local_scope_branch": self.controller.run_inputs.scope_branch,
            "remote_head_branch": self.controller.run_inputs.scope_branch,
            "observed_remote_head_before": (
                observed if existing is None else existing.observed_remote_head_before
            ),
            "draft": True,
            "pr_title": title,
            "pr_body": body,
            "pr_body_digest": sha256_digest(body),
            "operation_id": operation_id,
        }
        expected = PublishPlan.create(**values)
        if existing is not None and existing != expected:
            raise PublishEvidenceError("publish_plan_mismatch")
        plan = persist_once(self.run_path, "plan.json", expected, PublishPlan, self.context)
        self._verify_pr_set(plan, self._find(plan), allow_none=True)
        self.plan = plan
        return plan

    def execute(self, state: GraphState) -> PublishResult:
        if state.graph.current_node != "CREATE_PR":
            raise PublishEvidenceError("publish_state_mismatch")
        plan = self._load_plan_for_execution(state)
        request, decision = self.checkpoints().approved_for_create(state, plan)
        workspace = self._verify_local(plan.final_head)
        endpoint = self._verify_remote_identity(workspace, plan)

        restored = load_typed(self.run_path, "result.json", PublishResult, self.context)
        if restored is not None:
            self._verify_completed(plan, restored)
            self.result = restored
            return restored

        push = load_typed(self.run_path, "push.json", PushReceipt, self.context)
        before = self.controller.git.remote_branch_sha_at_endpoint(
            workspace, endpoint, plan.remote_head_branch
        )
        if before not in {None, plan.final_head}:
            raise PublishConflictError("remote_branch_conflict")
        if push is None:
            command = None
            performed = False
            if before is None:
                pushed = self.controller.git.push_exact_branch_to_endpoint(
                    workspace,
                    endpoint=endpoint,
                    commit_sha=plan.final_head,
                    remote_branch=plan.remote_head_branch,
                )
                performed = True
                command = SafeGitCommandReceipt(
                    pushed.receipt.command_id,
                    pushed.receipt.status.value,
                    pushed.receipt.exit_code,
                )
                self.controller.fault("after_publish_push")
            after = self.controller.git.remote_branch_sha_at_endpoint(
                workspace, endpoint, plan.remote_head_branch
            )
            if after != plan.final_head:
                raise PublishEvidenceError("remote_branch_verification_failed")
            push = PushReceipt.create(
                publish_plan_digest=plan.digest,
                operation_id=plan.operation_id,
                remote_repository_id=plan.remote_repository_id,
                remote_repository_full_name=plan.remote_repository_full_name,
                remote_branch=plan.remote_head_branch,
                expected_final_head=plan.final_head,
                observed_before_sha=before,
                observed_after_sha=after,
                performed_push=performed,
                command_receipt=command,
            )
            persist_once(self.run_path, "push.json", push, PushReceipt, self.context)
            self.controller.fault("after_publish_push_receipt")
        else:
            self._validate_push(plan, push, before)

        pr_receipt = load_typed(
            self.run_path, "pull-request.json", PullRequestReceipt, self.context
        )
        if pr_receipt is None:
            matches = self._find(plan)
            exact = self._verify_pr_set(plan, matches, allow_none=True)
            adopted = exact is not None
            remote_pr = exact
            if remote_pr is None:
                identity = self._identity(plan)
                remote_pr = self._provider().create_draft_pull_request(
                    DraftPullRequestRequest(
                        identity,
                        plan.pr_title,
                        plan.pr_body,
                        plan.remote_head_branch,
                        plan.base_branch,
                        plan.final_head,
                    )
                )
                self.controller.fault("after_publish_pr_create")
                self._validate_pr(plan, remote_pr)
            pr_receipt = PullRequestReceipt.create(
                publish_plan_digest=plan.digest,
                operation_id=plan.operation_id,
                remote_repository_id=plan.remote_repository_id,
                remote_repository_full_name=plan.remote_repository_full_name,
                pr_id=remote_pr.pr_id,
                pr_number=remote_pr.number,
                pr_url=remote_pr.url,
                draft=remote_pr.draft,
                head_branch=remote_pr.head_branch,
                head_sha=remote_pr.head_sha,
                base_branch=remote_pr.base_branch,
                adopted_existing=adopted,
            )
            persist_once(
                self.run_path,
                "pull-request.json",
                pr_receipt,
                PullRequestReceipt,
                self.context,
            )
            self.controller.fault("after_publish_pr_receipt")
        else:
            self._validate_pr_receipt(plan, pr_receipt)

        self._verify_local(plan.final_head)
        final_remote_head = self.controller.git.remote_branch_sha_at_endpoint(
            workspace, endpoint, plan.remote_head_branch
        )
        if final_remote_head != plan.final_head:
            raise PublishEvidenceError("remote_branch_verification_failed")

        result = PublishResult.create(
            project_id=plan.project_id,
            run_id=plan.run_id,
            scope_id=plan.scope_id,
            publish_plan_digest=plan.digest,
            checkpoint_id=request.checkpoint_id,
            checkpoint_request_digest=request.request_digest,
            checkpoint_decision_digest=decision.decision_digest,
            final_head=plan.final_head,
            remote_repository_full_name=plan.remote_repository_full_name,
            remote_branch=plan.remote_head_branch,
            remote_branch_head=final_remote_head,
            pr_id=pr_receipt.pr_id,
            pr_number=pr_receipt.pr_number,
            pr_url=pr_receipt.pr_url,
            draft=True,
            push_receipt_digest=push.digest,
            pr_receipt_digest=pr_receipt.digest,
        )
        persist_once(self.run_path, "result.json", result, PublishResult, self.context)
        self.controller.fault("after_publish_result")
        self.result = result
        return result

    def report(self, *, require_result: bool = False) -> PublishReport | None:
        result = self.result or load_typed(
            self.run_path, "result.json", PublishResult, self.context
        )
        if result is None:
            if require_result:
                raise PublishEvidenceError("publish_result_missing")
            return None
        plan = self.plan or load_typed(self.run_path, "plan.json", PublishPlan, self.context)
        if plan is None:
            raise PublishEvidenceError("publish_plan_missing")
        try:
            request = self.checkpoints().store.load_typed_request(
                result.checkpoint_id, PublishCheckpointRequestRecord
            )
        except CheckpointEvidenceError as exc:
            raise PublishEvidenceError("publish_checkpoint_evidence_invalid") from exc
        if request is None:
            raise PublishEvidenceError("publish_checkpoint_missing")
        try:
            decision = self.checkpoints()._load_decision(request)
        except CheckpointError as exc:
            raise PublishEvidenceError("publish_checkpoint_evidence_invalid") from exc
        if decision is None:
            raise PublishEvidenceError("publish_approval_missing")
        push = load_typed(self.run_path, "push.json", PushReceipt, self.context)
        pull_request = load_typed(
            self.run_path, "pull-request.json", PullRequestReceipt, self.context
        )
        if push is None or pull_request is None:
            raise PublishEvidenceError("publish_result_mismatch")
        verify_publish_evidence_chain(plan, request, decision, push, pull_request, result)
        self.result = result
        return PublishReport(
            result.remote_repository_full_name,
            plan.base_branch,
            result.remote_branch,
            result.final_head,
            result.pr_number,
            result.pr_url,
            result.draft,
            plan.digest,
        )

    def verify_result_for_recovery(self, state: GraphState) -> None:
        """Verify a completed external effect before replaying its recorded NodeResult."""

        plan = self._load_plan_for_execution(state)
        result = load_typed(self.run_path, "result.json", PublishResult, self.context)
        if result is None:
            raise PublishEvidenceError("publish_result_missing")
        request = self.checkpoints().store.load_typed_request(
            result.checkpoint_id, PublishCheckpointRequestRecord
        )
        if request is None:
            raise PublishEvidenceError("publish_checkpoint_missing")
        decision = self.checkpoints()._load_decision(request)
        push = load_typed(self.run_path, "push.json", PushReceipt, self.context)
        pull_request = load_typed(
            self.run_path, "pull-request.json", PullRequestReceipt, self.context
        )
        if decision is None or push is None or pull_request is None:
            raise PublishEvidenceError("publish_result_mismatch")
        verify_publish_evidence_chain(plan, request, decision, push, pull_request, result)
        self._verify_completed(plan, result)

    def checkpoints(self) -> PublishCheckpointController:
        return PublishCheckpointController(self, self.clock, self.nonce_factory)

    def assess_interrupted_side_effect(
        self, node_id: str, state: GraphState
    ) -> InterruptedSideEffectAssessment:
        if node_id != "CREATE_PR":
            return InterruptedSideEffectAssessment(False, "unreconciled_side_effect_capability")
        try:
            plan = self._load_plan_for_execution(state)
            self.checkpoints().approved_for_create(state, plan)
            self._verify_local(plan.final_head)
            workspace = self.controller.git.discover_repository(self.run_path / "workspace")
            endpoint = self._verify_remote_identity(workspace, plan)
            branch = self.controller.git.remote_branch_sha_at_endpoint(
                workspace, endpoint, plan.remote_head_branch
            )
            if branch not in {None, plan.final_head}:
                return InterruptedSideEffectAssessment(False, "remote_branch_conflict")
            matches = self._find(plan)
            self._verify_pr_set(plan, matches, allow_none=True)
            push = load_typed(self.run_path, "push.json", PushReceipt, self.context)
            pull_request = load_typed(
                self.run_path, "pull-request.json", PullRequestReceipt, self.context
            )
            result = load_typed(self.run_path, "result.json", PublishResult, self.context)
            if push is not None:
                self._validate_push(plan, push, branch)
            if pull_request is not None:
                self._validate_pr_receipt(plan, pull_request)
            if result is not None:
                request, decision = self.checkpoints().approved_for_create(state, plan)
                if push is None or pull_request is None:
                    raise PublishEvidenceError("publish_result_mismatch")
                verify_publish_evidence_chain(plan, request, decision, push, pull_request, result)
            return InterruptedSideEffectAssessment(
                True,
                "create_pr_reconciled_safe_rerun",
                {"remote_branch_head": branch, "matching_prs": len(matches)},
            )
        except RemoteServiceError:
            return InterruptedSideEffectAssessment(False, "publish_reconciliation_unavailable")
        except RemoteProviderError as exc:
            return InterruptedSideEffectAssessment(False, exc.code)
        except Exception as exc:
            return InterruptedSideEffectAssessment(
                False, getattr(exc, "code", "publish_reconciliation_unsafe")
            )

    def _delivery_authority(self, state):
        completed = self.controller.completed_reports(state)
        delivery = self.controller.delivery_reviews()
        delivery.rehydrate_if_complete(state)
        report = delivery.report
        if (
            report is None
            or report.verdict is not ReviewVerdict.PASS
            or not report.safe_to_create_pr
            or state.review.verdict is not ReviewVerdict.PASS
            or not state.review.safe_to_create_pr
        ):
            raise PublishEvidenceError("delivery_review_not_publishable")
        document = read_evidence(self.run_path / "delivery-review" / "manifest.json")
        manifest = decode_value(document.get("payload"), DeliveryManifest)
        if (
            manifest.digest != report.manifest_digest
            or manifest.final_head != report.final_head
            or manifest.final_tree_id != report.final_tree_id
            or tuple(item.commit_sha for item in completed) != manifest.completed_commit_shas
        ):
            raise PublishEvidenceError("delivery_review_evidence_mismatch")
        return manifest, report, completed

    def _verify_local(self, final_head: str):
        self.controller.verify_run_boundary_from_head(final_head)
        workspace = self.controller.git.discover_repository(self.run_path / "workspace")
        snapshot = self.controller.git.snapshot(workspace)
        branch_head = self.controller.git.resolve_ref(
            self.controller.target, f"refs/heads/{self.controller.run_inputs.scope_branch}"
        )
        if (
            snapshot.head_sha != final_head
            or snapshot.branch != self.controller.run_inputs.scope_branch
            or snapshot.dirty
            or snapshot.detached_head
            or branch_head != final_head
        ):
            raise WorkspaceError("publish_local_head_drift")
        return workspace

    def _push_endpoint(self, workspace, plan=None):
        remote_name = self.remote_name if plan is None else plan.remote_name
        urls = self.controller.git.remote_push_urls(workspace, remote_name)
        if len(urls) != 1:
            raise PublishPreparationError("multiple_push_targets_not_supported")
        endpoint = GitRemoteEndpoint(urls[0])
        reference = parse_github_remote_url(endpoint.url)
        identity = self._provider().inspect_repository(reference)
        if identity.full_name.lower() != reference.full_name.lower():
            raise PublishPreparationError("remote_repository_identity_mismatch")
        if plan is not None and (
            reference.host != plan.remote_host
            or reference.full_name.lower() != plan.remote_repository_full_name.lower()
            or identity != self._identity(plan)
        ):
            raise PublishEvidenceError("publish_remote_identity_mismatch")
        return endpoint, identity

    def _verify_remote_identity(self, workspace, plan):
        endpoint, identity = self._push_endpoint(workspace, plan)
        if identity != self._identity(plan):
            raise PublishEvidenceError("publish_remote_identity_mismatch")
        return endpoint

    def _provider(self) -> RemoteProvider:
        if self.provider is None:
            raise PublishPreparationError("remote_provider_required")
        return self.provider

    def _load_plan_for_execution(self, state):
        plan = load_typed(self.run_path, "plan.json", PublishPlan, self.context)
        if plan is None:
            raise PublishEvidenceError("publish_plan_missing")
        manifest, review, completed = self._delivery_authority(state)
        if (
            plan.final_head != manifest.final_head
            or plan.final_tree_id != manifest.final_tree_id
            or plan.delivery_manifest_digest != manifest.digest
            or plan.delivery_review_evidence_reference != review.evidence_reference
            or plan.source_revision != self.controller.run_inputs.source_revision
            or plan.work_plan_digest != self.controller.run_inputs.work_plan_digest
            or plan.target_baseline_head != self.controller.run_inputs.target_baseline_head
            or plan.local_scope_branch != self.controller.run_inputs.scope_branch
            or plan.remote_head_branch != self.controller.run_inputs.scope_branch
            or plan.base_branch != self.controller.run_inputs.base_branch
            or not completed
        ):
            raise PublishEvidenceError("publish_plan_mismatch")
        self.plan = plan
        return plan

    def _find(self, plan):
        return self._provider().find_open_pull_requests(
            self._identity(plan),
            head_branch=plan.remote_head_branch,
            base_branch=plan.base_branch,
        )

    @staticmethod
    def _identity(plan):
        return RemoteRepositoryIdentity(
            plan.remote_host, plan.remote_repository_id, plan.remote_repository_full_name
        )

    def _verify_pr_set(self, plan, matches, *, allow_none):
        if not matches:
            if allow_none:
                return None
            raise PublishConflictError("pull_request_conflict")
        if len(matches) != 1:
            raise PublishConflictError("pull_request_conflict")
        self._validate_pr(plan, matches[0])
        return matches[0]

    def _validate_pr(self, plan, pull_request):
        if (
            pull_request.repository != self._identity(plan)
            or pull_request.head_branch != plan.remote_head_branch
            or pull_request.base_branch != plan.base_branch
            or pull_request.head_sha != plan.final_head
            or not pull_request.draft
            or pull_request.title != plan.pr_title
            or pull_request.body != plan.pr_body
            or plan.marker not in pull_request.body
        ):
            raise PublishConflictError("pull_request_conflict")

    def _validate_push(self, plan, receipt, live):
        if (
            receipt.publish_plan_digest != plan.digest
            or receipt.operation_id != plan.operation_id
            or receipt.remote_repository_id != plan.remote_repository_id
            or receipt.remote_repository_full_name != plan.remote_repository_full_name
            or receipt.remote_branch != plan.remote_head_branch
            or receipt.expected_final_head != plan.final_head
            or receipt.observed_after_sha != plan.final_head
            or live != plan.final_head
        ):
            raise PublishEvidenceError("push_receipt_mismatch")

    def _validate_pr_receipt(self, plan, receipt):
        if (
            receipt.publish_plan_digest != plan.digest
            or receipt.operation_id != plan.operation_id
            or receipt.remote_repository_id != plan.remote_repository_id
            or receipt.remote_repository_full_name != plan.remote_repository_full_name
            or receipt.head_branch != plan.remote_head_branch
            or receipt.head_sha != plan.final_head
            or receipt.base_branch != plan.base_branch
            or not receipt.draft
        ):
            raise PublishEvidenceError("pull_request_receipt_mismatch")
        matches = self._find(plan)
        exact = self._verify_pr_set(plan, matches, allow_none=False)
        if (
            exact.pr_id != receipt.pr_id
            or exact.number != receipt.pr_number
            or exact.url != receipt.pr_url
            or exact.repository.full_name != receipt.remote_repository_full_name
            or exact.head_branch != receipt.head_branch
            or exact.head_sha != receipt.head_sha
            or exact.base_branch != receipt.base_branch
            or exact.draft != receipt.draft
        ):
            raise PublishEvidenceError("pull_request_receipt_mismatch")

    def _verify_completed(self, plan, result):
        workspace = self._verify_local(plan.final_head)
        endpoint = self._verify_remote_identity(workspace, plan)
        live = self.controller.git.remote_branch_sha_at_endpoint(
            workspace, endpoint, plan.remote_head_branch
        )
        push = load_typed(self.run_path, "push.json", PushReceipt, self.context)
        pr = load_typed(self.run_path, "pull-request.json", PullRequestReceipt, self.context)
        if push is None or pr is None:
            raise PublishEvidenceError("publish_result_mismatch")
        self._validate_push(plan, push, live)
        self._validate_pr_receipt(plan, pr)
        request = self.checkpoints().store.load_typed_request(
            result.checkpoint_id, PublishCheckpointRequestRecord
        )
        if request is None:
            raise PublishEvidenceError("publish_checkpoint_missing")
        decision = self.checkpoints()._load_decision(request)
        if decision is None:
            raise PublishEvidenceError("publish_result_mismatch")
        verify_publish_evidence_chain(plan, request, decision, push, pr, result)

    @staticmethod
    def _require_publish_checkpoint(state):
        if (
            state.graph.current_node != "HUMAN_CHECKPOINT"
            or state.graph.pending_resume_node != "CREATE_PR"
            or state.run.status.value != "running"
        ):
            raise PublishPreparationError("publish_checkpoint_not_pending")


class PublishCheckpointController:
    def __init__(self, execution, clock=utc_now, nonce_factory=None):
        self.execution = execution
        self.clock = clock
        self.store = CheckpointStore(execution.run_path, clock=clock, nonce_factory=nonce_factory)

    @staticmethod
    def checkpoint_id(state):
        return f"checkpoint-{state.state_version}-create_pr"

    def ensure_request(self, state, plan=None):
        PublishExecution._require_publish_checkpoint(state)
        plan = plan or self.execution.ensure_plan(state)
        checkpoint_id = self.checkpoint_id(state)
        binding = self._binding(state, plan, checkpoint_id)
        try:
            existing = self.store.load_typed_request(checkpoint_id, PublishCheckpointRequestRecord)
            if existing is not None:
                self._validate(existing, binding)
                return existing
            now = self._now()
            request = PublishCheckpointRequestRecord.create(
                **binding,
                nonce=self.store.new_nonce(),
                created_at=format_timestamp(now),
                expires_at=format_timestamp(
                    now
                    + timedelta(seconds=self.execution.controller.run_inputs.checkpoint_ttl_seconds)
                ),
            )
            persisted = self.store.write_typed_request_once(request, PublishCheckpointRequestRecord)
            self._validate(persisted, binding)
            return persisted
        except CheckpointEvidenceError as exc:
            raise CheckpointBindingError("checkpoint_evidence_invalid") from exc
        except CheckpointStoreError as exc:
            raise CheckpointError(str(exc)) from exc

    def view(self, request, plan):
        return PublishCheckpointView(
            request.checkpoint_id,
            request.nonce,
            request.created_at,
            request.expires_at,
            plan.remote_repository_full_name,
            plan.base_branch,
            plan.remote_head_branch,
            plan.final_head,
            True,
            plan.pr_title,
            plan.digest,
        )

    def decision(self, state, plan=None):
        request = self.ensure_request(state, plan)
        return request, self._load_decision(request)

    def decision_for_consumption(self, state):
        """Load a persisted decision using durable local bindings only."""

        PublishExecution._require_publish_checkpoint(state)
        plan = load_typed(
            self.execution.run_path,
            "plan.json",
            PublishPlan,
            self.execution.context,
        )
        if plan is None:
            return None, None, None
        run = self.execution.controller.run_inputs
        if (
            plan.project_id != run.project_id
            or plan.run_id != self.execution.controller.run_id
            or plan.scope_id != run.scope_id
            or plan.source_revision != run.source_revision
            or plan.work_plan_digest != run.work_plan_digest
            or plan.target_baseline_head != run.target_baseline_head
            or plan.local_scope_branch != run.scope_branch
            or plan.remote_head_branch != run.scope_branch
            or plan.base_branch != run.base_branch
        ):
            raise PublishEvidenceError("publish_plan_mismatch")
        checkpoint_id = self.checkpoint_id(state)
        try:
            request = self.store.load_typed_request(checkpoint_id, PublishCheckpointRequestRecord)
        except CheckpointEvidenceError as exc:
            raise CheckpointBindingError("checkpoint_evidence_invalid") from exc
        if request is None:
            return plan, None, None
        self._validate(request, self._binding(state, plan, checkpoint_id))
        return plan, request, self._load_decision(request)

    def submit(self, state, *, checkpoint_id, nonce, outcome, actor):
        request = self.ensure_request(state)
        if checkpoint_id != request.checkpoint_id:
            raise CheckpointError("checkpoint_not_pending")
        if self.store.load_decision(checkpoint_id) is not None:
            raise CheckpointError("checkpoint_already_decided")
        if nonce != request.nonce:
            raise CheckpointError("checkpoint_nonce_mismatch")
        if not isinstance(outcome, CheckpointOutcome):
            raise CheckpointError("checkpoint_outcome_invalid")
        if not isinstance(actor, str) or not actor.strip() or "\x00" in actor or len(actor) > 256:
            raise CheckpointError("checkpoint_actor_invalid")
        now = self._now()
        if now < parse_timestamp(request.created_at):
            raise CheckpointError("checkpoint_time_invalid")
        if now > parse_timestamp(request.expires_at):
            raise CheckpointError("checkpoint_expired")
        decision = CheckpointDecision.create(
            schema_version=1,
            checkpoint_id=checkpoint_id,
            request_digest=request.request_digest,
            nonce=request.nonce,
            outcome=outcome,
            actor=actor,
            decided_at=format_timestamp(now),
        )
        try:
            self.store.write_decision_once(decision)
        except CheckpointStoreError as exc:
            raise CheckpointError(str(exc)) from exc
        return decision

    def approved_for_create(self, state, plan):
        checkpoint_id = f"checkpoint-{state.state_version - 1}-create_pr"
        try:
            request = self.store.load_typed_request(checkpoint_id, PublishCheckpointRequestRecord)
        except CheckpointEvidenceError as exc:
            raise PublishEvidenceError("publish_checkpoint_evidence_invalid") from exc
        if request is None:
            raise PublishEvidenceError("publish_checkpoint_missing")
        self._validate_plan_binding(request, plan, state)
        decision = self._load_decision(request)
        if decision is None or decision.outcome is not CheckpointOutcome.APPROVED:
            raise PublishEvidenceError("publish_approval_missing")
        return request, decision

    def expired(self, request):
        return self._now() > parse_timestamp(request.expires_at)

    def decision_reference(self, request):
        return self.store.relative_decision_reference(request.checkpoint_id)

    def _load_decision(self, request):
        try:
            decision = self.store.load_decision(request.checkpoint_id)
        except CheckpointEvidenceError as exc:
            raise CheckpointBindingError("checkpoint_evidence_invalid") from exc
        if decision is not None and (
            decision.request_digest != request.request_digest
            or decision.nonce != request.nonce
            or parse_timestamp(decision.decided_at) < parse_timestamp(request.created_at)
            or parse_timestamp(decision.decided_at) > parse_timestamp(request.expires_at)
        ):
            raise CheckpointBindingError("checkpoint_binding_mismatch")
        return decision

    def _binding(self, state, plan, checkpoint_id):
        return {
            "schema_version": 1,
            "checkpoint_id": checkpoint_id,
            "project_id": plan.project_id,
            "run_id": plan.run_id,
            "code": PUBLISH_CHECKPOINT_CODE,
            "message": PUBLISH_CHECKPOINT_MESSAGE,
            "node_id": "HUMAN_CHECKPOINT",
            "pending_resume_node": "CREATE_PR",
            "state_version": state.state_version,
            "state_digest": StateStore.digest_for_state(state),
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
            "pr_title_digest": sha256_digest(plan.pr_title),
            "pr_body_digest": plan.pr_body_digest,
        }

    def _validate(self, request, binding):
        if any(getattr(request, key) != value for key, value in binding.items()):
            raise CheckpointBindingError("checkpoint_binding_mismatch")
        lifetime = parse_timestamp(request.expires_at) - parse_timestamp(request.created_at)
        if lifetime != timedelta(
            seconds=self.execution.controller.run_inputs.checkpoint_ttl_seconds
        ):
            raise CheckpointBindingError("checkpoint_binding_mismatch")

    @staticmethod
    def _validate_plan_binding(request, plan, state):
        prior_state = replace(
            state,
            state_version=state.state_version - 1,
            graph=replace(
                state.graph,
                current_node="HUMAN_CHECKPOINT",
                previous_node="DELIVERY_REVIEW",
                transition_seq=state.graph.transition_seq - 1,
                pending_resume_node="CREATE_PR",
            ),
        )
        expected = {
            "checkpoint_id": f"checkpoint-{state.state_version - 1}-create_pr",
            "code": PUBLISH_CHECKPOINT_CODE,
            "message": PUBLISH_CHECKPOINT_MESSAGE,
            "node_id": "HUMAN_CHECKPOINT",
            "pending_resume_node": "CREATE_PR",
            "state_version": state.state_version - 1,
            "state_digest": StateStore.digest_for_state(prior_state),
            **PublishCheckpointController._static_plan_binding(plan),
        }
        if any(getattr(request, key) != value for key, value in expected.items()):
            raise PublishEvidenceError("publish_checkpoint_binding_mismatch")

    @staticmethod
    def _static_plan_binding(plan):
        return {
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
            "pr_title_digest": sha256_digest(plan.pr_title),
            "pr_body_digest": plan.pr_body_digest,
        }

    @staticmethod
    def _validate_static_plan_binding(request, plan):
        expected = PublishCheckpointController._static_plan_binding(plan)
        if any(getattr(request, key) != value for key, value in expected.items()):
            raise PublishEvidenceError("publish_checkpoint_binding_mismatch")

    def _now(self):
        now = self.clock()
        format_timestamp(now)
        return now


@dataclass(frozen=True, slots=True)
class HumanCheckpointDispatchNode:
    controller: object
    node_id: str = "HUMAN_CHECKPOINT"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        if state.graph.pending_resume_node == "IMPLEMENT":
            from agentgraph.nodes import HumanCheckpointNode

            execution = self.controller.activate(state)
            return HumanCheckpointNode(self.controller.checkpoints_for_execution(execution)).run(
                state, context
            )
        if state.graph.pending_resume_node != "CREATE_PR":
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.BLOCKED,
                reason=ResultReason("checkpoint_not_pending", "Unsupported checkpoint route."),
            )
        publish = self.controller.publication()
        try:
            _local_plan, local_request, local_decision = (
                publish.checkpoints().decision_for_consumption(state)
            )
            if local_decision is not None:
                return NodeResult(
                    self.node_id,
                    context.node_attempt_id,
                    NodeStatus.SUCCEEDED,
                    checkpoint_outcome=local_decision.outcome,
                    evidence=(
                        Evidence(
                            "human_checkpoint",
                            publish.checkpoints().decision_reference(local_request),
                        ),
                    ),
                )
            plan = publish.ensure_plan(state)
            request, decision = publish.checkpoints().decision(state, plan)
        except Exception as exc:
            publish.issue_code = getattr(exc, "code", "publish_checkpoint_evidence_invalid")
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.BLOCKED,
                reason=ResultReason(publish.issue_code, str(exc)),
            )
        if decision is None:
            if publish.checkpoints().expired(request):
                publish.issue_code = "checkpoint_expired"
                return NodeResult(
                    self.node_id,
                    context.node_attempt_id,
                    NodeStatus.BLOCKED,
                    reason=ResultReason("checkpoint_expired", "Publish approval expired."),
                )
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.CHECKPOINT_REQUIRED,
                checkpoint_request=CheckpointRequest(request.code, request.message),
            )
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            checkpoint_outcome=decision.outcome,
            evidence=(
                Evidence("human_checkpoint", publish.checkpoints().decision_reference(request)),
            ),
        )


@dataclass(frozen=True, slots=True)
class CreatePullRequestNode:
    execution: PublishExecution
    node_id: str = "CREATE_PR"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            result = self.execution.execute(state)
        except PublishConflictError as exc:
            self.execution.issue_code = exc.code
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.BLOCKED,
                reason=ResultReason(exc.code, str(exc)),
            )
        except RemoteServiceError as exc:
            self.execution.issue_code = exc.code
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.FAILED,
                reason=ResultReason(exc.code, str(exc)),
                failure_category=FailureCategory.EXTERNAL_SERVICE,
            )
        except GitError as exc:
            self.execution.issue_code = "publish_git_failed"
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.FAILED,
                reason=ResultReason("publish_git_failed", str(exc)),
                failure_category=FailureCategory.ENVIRONMENT,
            )
        except (
            RemoteContractError,
            UnsafeRemoteUrlError,
            UnsupportedRemoteHostError,
            PublishEvidenceError,
            WorkspaceError,
        ) as exc:
            self.execution.issue_code = getattr(exc, "code", "publish_contract_invalid")
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.FAILED,
                reason=ResultReason(self.execution.issue_code, str(exc)),
                failure_category=FailureCategory.CONTRACT,
            )
        except RemoteProviderError as exc:
            self.execution.issue_code = exc.code
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.FAILED,
                reason=ResultReason(exc.code, str(exc)),
                failure_category=FailureCategory.EXTERNAL_SERVICE,
            )
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            evidence=(Evidence("publish_result", "publish/result.json"),),
            external_effects=(
                ExternalEffect("remote_branch_push", result.remote_branch),
                ExternalEffect("draft_pull_request", result.pr_url),
            ),
        )
