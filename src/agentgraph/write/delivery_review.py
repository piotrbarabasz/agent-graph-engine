"""Independent cumulative delivery review outside Core and item execution."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentgraph.agents import (
    DELIVERY_REVIEW_SCHEMA,
    AgentAnalysisStatus,
    AgentContext,
    AgentError,
    AgentMutationError,
    AgentProvider,
    AgentRequest,
    AgentResponse,
    AgentResponseContractError,
    DeliveryReviewAnalysis,
    DeliveryReviewFinding,
    DeliveryReviewFindingKind,
    DeliveryReviewVerdict,
    parse_delivery_review_payload,
)
from agentgraph.agents.prompts import build_delivery_review_prompt
from agentgraph.core import (
    FailureCategory,
    GraphState,
    NodeContext,
    NodeResult,
    NodeStatus,
    PatchOperation,
    ResultReason,
    ReviewVerdict,
    RunStatus,
    StatePatch,
)
from agentgraph.runtime.codec import decode_value, encode_value, sha256_digest

from .capability import normalize_repo_path, path_is_allowed, stable_path_union
from .delivery_storage import verify_delivery_storage
from .errors import (
    DeliveryReviewContextError,
    DeliveryReviewEvidenceError,
    DeliveryReviewStateMismatchError,
    WorkspaceError,
)
from .evidence import read_evidence, write_evidence
from .models import (
    DeliveryCompletedItem,
    DeliveryManifest,
    DeliveryManifestEntry,
    DeliveryReviewContext,
    DeliveryReviewReport,
)

if TYPE_CHECKING:
    from .multi_item import MultiItemExecution


_INVARIANTS = (
    "cumulative_delivery_from_original_target_baseline",
    "all_planned_items_verified",
    "target_main_worktree_read_only",
    "external_scope_workspace_clean",
    "scope_branch_pinned_to_final_head",
    "delivery_reviewer_read_only",
    "no_delivery_repair",
    "no_push_or_pull_request",
)


def delivery_review_request(context: DeliveryReviewContext) -> AgentRequest:
    """Build the one canonical request used by invocation and recovery."""

    return AgentRequest.create(
        "delivery_review",
        build_delivery_review_prompt(context),
        DELIVERY_REVIEW_SCHEMA,
        "agentgraph.delivery-review.v1",
    )


@dataclass(slots=True)
class DeliveryReviewExecution:
    controller: MultiItemExecution
    provider: AgentProvider
    report: DeliveryReviewReport | None = field(default=None, init=False)
    issue_code: str | None = field(default=None, init=False)

    @property
    def root(self) -> Path:
        return self.controller.run_path / "delivery-review"

    def review(
        self, state: GraphState, node_attempt_id: str
    ) -> tuple[NodeStatus, ReviewVerdict | None, tuple[str, ...], str | None]:
        review_path = self.root / "review.json"
        verify_delivery_storage(self.controller.run_path, review_path)
        if review_path.exists():
            analysis = self._restore_completed(state)
            if analysis.status is AgentAnalysisStatus.BLOCKED:
                return NodeStatus.BLOCKED, None, (), analysis.reason_code
            assert analysis.verdict is not None
            verdict = (
                ReviewVerdict.PASS
                if analysis.verdict is DeliveryReviewVerdict.PASS
                else ReviewVerdict.FAIL
            )
            return NodeStatus.SUCCEEDED, verdict, self._findings(analysis), None

        manifest, context, mechanical_passed, mechanical_findings = self._prepare(state)
        if not mechanical_passed:
            analysis = self._mechanical_analysis(mechanical_findings)
            self._write_review(
                manifest,
                context,
                analysis,
                evidence_reference=None,
                input_digest=None,
                output_digest=None,
                mechanical_findings=mechanical_findings,
            )
            self.report = self._report(
                manifest, ReviewVerdict.FAIL, mechanical_findings, "delivery-review/review.json"
            )
            return NodeStatus.SUCCEEDED, ReviewVerdict.FAIL, mechanical_findings, None

        request = delivery_review_request(context)
        analysis, reference, output_digest = self._invoke(
            request, context, manifest, node_attempt_id
        )
        self.controller.fault("after_delivery_review_provider_evidence")
        self._write_review(
            manifest,
            context,
            analysis,
            evidence_reference=reference,
            input_digest=request.input_digest,
            output_digest=output_digest,
            mechanical_findings=(),
        )
        self.controller.fault("after_delivery_review_evidence")
        if analysis.status is AgentAnalysisStatus.BLOCKED:
            self.issue_code = analysis.reason_code
            return NodeStatus.BLOCKED, None, (), analysis.reason_code
        assert analysis.verdict is not None
        verdict = (
            ReviewVerdict.PASS
            if analysis.verdict is DeliveryReviewVerdict.PASS
            else ReviewVerdict.FAIL
        )
        findings = self._findings(analysis)
        self.report = self._report(manifest, verdict, findings, "delivery-review/review.json")
        return NodeStatus.SUCCEEDED, verdict, findings, None

    def rehydrate_if_complete(self, state: GraphState) -> None:
        review_path = self.root / "review.json"
        verify_delivery_storage(self.controller.run_path, review_path)
        if review_path.exists():
            self._restore_completed(state)

    def _prepare(
        self, state: GraphState
    ) -> tuple[DeliveryManifest, DeliveryReviewContext, bool, tuple[str, ...]]:
        verify_delivery_storage(self.controller.run_path)
        self.root.mkdir(parents=True, exist_ok=True)
        verify_delivery_storage(self.controller.run_path)
        completed = self.controller.completed_reports(state)
        if not completed:
            raise WorkspaceError("delivery review has no verified completed items")
        final_head = completed[-1].commit_sha
        findings: list[str] = []
        try:
            self.controller.verify_run_boundary_from_head(final_head)
            if len(completed) != len(self.controller.plan.items) or {
                item.item_id for item in completed
            } != {item.item_id for item in self.controller.plan.items}:
                findings.append("incomplete_delivery: not all frozen work-plan items completed")
            workspace = self.controller.git.discover_repository(
                self.controller.run_path / "workspace"
            )
            snapshot = self.controller.git.snapshot(workspace)
            scope_head = self.controller.git.resolve_ref(
                self.controller.target, f"refs/heads/{self.controller.run_inputs.scope_branch}"
            )
            if (
                snapshot.head_sha != final_head
                or snapshot.branch != self.controller.run_inputs.scope_branch
                or snapshot.detached_head
                or snapshot.dirty
                or snapshot.staged_paths
                or snapshot.conflicted_paths
                or scope_head != final_head
            ):
                findings.append("delivery_workspace_drift: final scope workspace is not pinned")
            manifest = self._manifest(completed, final_head)
            allowed = stable_path_union(
                *(item.allowed_paths for item in self.controller.plan.items)
            )
            for entry in manifest.changed_files:
                if not path_is_allowed(entry.path, allowed):
                    findings.append(
                        f"delivery_scope_violation: path outside delivery capability: {entry.path}"
                    )
            diff_check = self.controller.git.diff_check_between(
                self.controller.target,
                self.controller.run_inputs.target_baseline_head,
                final_head,
            )
            if not diff_check.ok:
                findings.append(
                    "delivery_diff_check_failed: cumulative Git diff has whitespace errors"
                )
        except Exception as exc:
            if not findings:
                findings.append(f"delivery_mechanical_gate_failed: {type(exc).__name__}")
            manifest = self._manifest(completed, final_head)
            diff_check = None

        context = self._context(manifest, completed)
        self._persist_or_verify("manifest.json", manifest, DeliveryManifest)
        self._persist_or_verify("context.json", context, DeliveryReviewContext)
        mechanical = {
            "passed": not findings,
            "findings": tuple(findings),
            "diff_check_ok": None if diff_check is None else diff_check.ok,
            "manifest_digest": manifest.digest,
            "context_digest": context.context_digest,
        }
        self._persist_or_verify_payload("mechanical-review.json", mechanical)
        return manifest, context, not findings, tuple(findings)

    def _manifest(self, completed, final_head: str) -> DeliveryManifest:
        run = self.controller.run_inputs
        entries = []
        for path in self.controller.git.diff_paths_between(
            self.controller.target, run.target_baseline_head, final_head
        ):
            name = path.as_posix()
            normalize_repo_path(name)
            entry = self.controller.git.tree_entry(self.controller.target, final_head, name)
            if (
                entry is None
                or entry.object_type != "blob"
                or entry.mode not in {"100644", "100755"}
            ):
                raise WorkspaceError(f"unsupported final delivery tree entry: {name}")
            raw = self.controller.git.read_blob(self.controller.target, entry.object_id)
            entries.append(
                DeliveryManifestEntry(
                    name,
                    hashlib.sha256(raw).hexdigest(),
                    len(raw),
                    0o755 if entry.mode == "100755" else 0o644,
                    entry.object_id,
                )
            )
        return DeliveryManifest.create(
            scope_id=run.scope_id,
            target_baseline_head=run.target_baseline_head,
            final_head=final_head,
            final_tree_id=self.controller.git.commit_tree_id(self.controller.target, final_head),
            changed_files=tuple(entries),
            completed_item_ids=tuple(item.item_id for item in completed),
            completed_commit_shas=tuple(item.commit_sha for item in completed),
            work_plan_digest=self.controller.plan.digest,
            source_revision=run.source_revision,
        )

    def _context(self, manifest: DeliveryManifest, completed) -> DeliveryReviewContext:
        reports = {item.item_id: item for item in completed}

        def summary(plan_item) -> DeliveryCompletedItem:
            report = reports[plan_item.item_id]
            package = plan_item.package
            return DeliveryCompletedItem(
                plan_item.item_id,
                plan_item.plan_index,
                package.title,
                package.goal,
                package.acceptance_criteria,
                package.test_requirements,
                report.item_base_head,
                report.commit_sha,
                report.changed_paths,
                report.repair_count,
            )

        by_id = {item.item_id: item for item in self.controller.plan.items}
        execution_order = tuple(summary(by_id[item.item_id]) for item in completed)
        declared = tuple(summary(item) for item in self.controller.plan.items)
        allowed = stable_path_union(*(item.allowed_paths for item in self.controller.plan.items))
        return DeliveryReviewContext.create(
            scope_id=self.controller.plan.scope_id,
            source_revision=self.controller.run_inputs.source_revision,
            work_plan_digest=self.controller.plan.digest,
            target_baseline_head=self.controller.run_inputs.target_baseline_head,
            final_head=manifest.final_head,
            final_tree_id=manifest.final_tree_id,
            delivery_manifest_digest=manifest.digest,
            final_changed_paths=tuple(item.path for item in manifest.changed_files),
            delivery_allowed_paths=allowed,
            completed_items=execution_order,
            declared_work=declared,
            architecture_invariants=_INVARIANTS,
        )

    def _invoke(self, request, context, manifest, node_attempt_id):
        before_workspace, before_target = self._guard(context.final_head)
        invocation_id, attempt = self._attempt_directory(node_attempt_id)
        agent_context = AgentContext(
            self.controller.run_inputs.project_id,
            self.controller.run_id,
            "DELIVERY_REVIEW",
            node_attempt_id,
            self.controller.run_path / "workspace",
            attempt,
            self.controller.run_inputs.target_baseline_head,
            self.controller.run_inputs.source_revision,
            invocation_id,
        )
        provider_error = None
        response = None
        try:
            response = self.provider.invoke(request, agent_context)
        except BaseException as exc:
            provider_error = exc
        try:
            after_workspace, after_target = self._guard(context.final_head)
        except Exception as exc:
            raise AgentMutationError("delivery reviewer mutated protected state") from (
                provider_error or exc
            )
        if after_workspace != before_workspace or after_target != before_target:
            raise AgentMutationError(
                "delivery reviewer mutated protected state"
            ) from provider_error
        if provider_error is not None:
            raise provider_error
        if not isinstance(response, AgentResponse) or response.input_digest != request.input_digest:
            raise AgentResponseContractError("delivery review response binding is invalid")
        analysis = parse_delivery_review_payload(dict(response.payload))
        self._validate_analysis(analysis)
        if response.output_digest != sha256_digest(encode_value(analysis)):
            raise AgentResponseContractError("delivery review output digest is invalid")
        reference = self._persist_host_evidence(
            attempt, invocation_id, request, response, analysis, context, manifest
        )
        return analysis, reference, response.output_digest

    def _guard(self, final_head: str):
        self.controller.verify_run_boundary_from_head(final_head)
        workspace = self.controller.git.discover_repository(self.controller.run_path / "workspace")
        return self.controller.git.snapshot(workspace), self.controller.git.snapshot(
            self.controller.target
        )

    def _attempt_directory(self, attempt_id: str) -> tuple[str, Path]:
        namespace = getattr(self.provider, "evidence_namespace", "neutral")
        if (
            not isinstance(namespace, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", namespace) is None
        ):
            raise AgentResponseContractError("delivery reviewer evidence namespace is invalid")
        root = self.root / "provider" / namespace / "agents" / "DELIVERY_REVIEW"
        verify_delivery_storage(self.controller.run_path, root)
        root.mkdir(parents=True, exist_ok=True)
        verify_delivery_storage(self.controller.run_path, root)
        safe = sha256_digest(attempt_id).removeprefix("sha256:")[:16]
        selected = safe
        index = 1
        while (root / selected).exists() or (root / selected).is_symlink():
            index += 1
            selected = f"{safe}-rerun-{index}"
        return selected, root / selected

    def _persist_host_evidence(
        self, attempt, invocation_id, request, response, analysis, context, manifest
    ) -> str:
        verify_delivery_storage(self.controller.run_path, attempt)
        attempt.mkdir(parents=True, exist_ok=True)
        path = attempt / "analysis.json"
        verify_delivery_storage(self.controller.run_path, path)
        write_evidence(
            path,
            context={**self._binding(manifest, context), "node_id": "DELIVERY_REVIEW"},
            payload={
                "provider_invocation_id": invocation_id,
                "provider": response.provider_name,
                "provider_version": response.provider_version,
                "model": response.model,
                "input_digest": request.input_digest,
                "output_digest": response.output_digest,
                "response": analysis,
                "provider_evidence": response.evidence_reference,
                "command_receipt": response.command_receipt,
            },
        )
        return path.relative_to(self.controller.run_path).as_posix()

    def _write_review(
        self,
        manifest,
        context,
        analysis,
        *,
        evidence_reference,
        input_digest,
        output_digest,
        mechanical_findings,
    ) -> None:
        write_evidence(
            self.root / "review.json",
            context=self._binding(manifest, context),
            payload={
                "analysis": analysis,
                "evidence_reference": evidence_reference,
                "input_digest": input_digest,
                "output_digest": output_digest,
                "mechanical_findings": mechanical_findings,
            },
        )

    def _restore_completed(self, state: GraphState) -> DeliveryReviewAnalysis:
        manifest, context, passed, findings = self._prepare(state)
        try:
            document = read_evidence(self.root / "review.json")
            if any(
                document.get(key) != value
                for key, value in self._binding(manifest, context).items()
            ):
                raise ValueError("binding")
            payload = document.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("payload")
            analysis = decode_value(payload["analysis"], DeliveryReviewAnalysis)
            if findings:
                expected_analysis = self._mechanical_analysis(findings)
                if (
                    payload.get("mechanical_findings") != findings
                    or analysis != expected_analysis
                    or payload.get("evidence_reference") is not None
                    or payload.get("input_digest") is not None
                    or payload.get("output_digest") is not None
                ):
                    raise ValueError("mechanical findings")
                self.report = self._report(
                    manifest, ReviewVerdict.FAIL, findings, "delivery-review/review.json"
                )
                self._reconcile_state(state, analysis, findings)
                return analysis
            if not passed:
                raise ValueError("mechanical")
            request = delivery_review_request(context)
            if payload.get("input_digest") != request.input_digest:
                raise ValueError("input digest")
            reference = payload.get("evidence_reference")
            output_digest = payload.get("output_digest")
            restored = self._restore_host(reference, request, context, manifest, output_digest)
            if restored != analysis:
                raise ValueError("analysis")
            if analysis.status is AgentAnalysisStatus.SUCCESS:
                verdict = (
                    ReviewVerdict.PASS
                    if analysis.verdict is DeliveryReviewVerdict.PASS
                    else ReviewVerdict.FAIL
                )
                self.report = self._report(
                    manifest, verdict, self._findings(analysis), "delivery-review/review.json"
                )
            self._reconcile_state(state, analysis, self._findings(analysis))
            return analysis
        except DeliveryReviewStateMismatchError:
            raise
        except DeliveryReviewEvidenceError:
            raise
        except Exception as exc:
            raise DeliveryReviewEvidenceError("delivery_review_evidence_mismatch") from exc

    def _restore_host(self, reference, request, context, manifest, output_digest):
        if not isinstance(reference, str) or not isinstance(output_digest, str):
            raise DeliveryReviewEvidenceError("delivery_review_evidence_mismatch")
        path = self.controller.run_path.joinpath(*reference.split("/"))
        verify_delivery_storage(self.controller.run_path, path)
        document = read_evidence(path)
        if (
            any(
                document.get(key) != value
                for key, value in self._binding(manifest, context).items()
            )
            or document.get("node_id") != "DELIVERY_REVIEW"
        ):
            raise DeliveryReviewEvidenceError("delivery_review_evidence_mismatch")
        payload = document.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("input_digest") != request.input_digest
            or payload.get("output_digest") != output_digest
        ):
            raise DeliveryReviewEvidenceError("delivery_review_evidence_mismatch")
        analysis = parse_delivery_review_payload(payload.get("response"))
        self._validate_analysis(analysis)
        if sha256_digest(encode_value(analysis)) != output_digest:
            raise DeliveryReviewEvidenceError("delivery_review_evidence_mismatch")
        return analysis

    def _persist_or_verify(self, name: str, value, value_type) -> None:
        path = self.root / name
        verify_delivery_storage(self.controller.run_path, path)
        if path.exists():
            try:
                document = read_evidence(path)
                if any(
                    document.get(key) != expected
                    for key, expected in self._authority_context().items()
                ):
                    raise ValueError("authority")
                restored = decode_value(document.get("payload"), value_type)
            except Exception as exc:
                raise DeliveryReviewContextError("delivery_review_context_mismatch") from exc
            if restored != value:
                raise DeliveryReviewContextError("delivery_review_context_mismatch")
            return
        write_evidence(path, context=self._authority_context(), payload=value)

    def _persist_or_verify_payload(self, name: str, value: dict[str, Any]) -> None:
        path = self.root / name
        verify_delivery_storage(self.controller.run_path, path)
        if path.exists():
            document = read_evidence(path)
            if any(
                document.get(key) != expected for key, expected in self._authority_context().items()
            ) or document.get("payload") != encode_value(value):
                raise DeliveryReviewEvidenceError("delivery_review_evidence_mismatch")
            return
        write_evidence(path, context=self._authority_context(), payload=value)

    def _authority_context(self) -> dict[str, Any]:
        run = self.controller.run_inputs
        return {
            "project_id": run.project_id,
            "run_id": self.controller.run_id,
            "scope_id": run.scope_id,
            "source_revision": run.source_revision,
            "work_plan_digest": run.work_plan_digest,
            "target_baseline_head": run.target_baseline_head,
        }

    def _binding(self, manifest, context) -> dict[str, Any]:
        return {
            **self._authority_context(),
            "final_head": manifest.final_head,
            "final_tree_id": manifest.final_tree_id,
            "delivery_manifest_digest": manifest.digest,
            "context_digest": context.context_digest,
        }

    @staticmethod
    def _findings(analysis: DeliveryReviewAnalysis) -> tuple[str, ...]:
        return tuple(f"{item.kind.value}: {item.message}" for item in analysis.findings)

    def _validate_analysis(self, analysis: DeliveryReviewAnalysis) -> None:
        known = {item.item_id for item in self.controller.plan.items}
        if any(
            item_id not in known for finding in analysis.findings for item_id in finding.item_ids
        ):
            raise AgentResponseContractError("delivery review finding references an unknown item")

    @staticmethod
    def _reconcile_state(
        state: GraphState, analysis: DeliveryReviewAnalysis, findings: tuple[str, ...]
    ) -> None:
        review = state.review
        before_node_result = (
            state.graph.current_node == "DELIVERY_REVIEW"
            and review.verdict is ReviewVerdict.UNKNOWN
            and review.safe_to_create_pr is False
            and review.findings == ()
        )
        if before_node_result:
            return

        if analysis.status is AgentAnalysisStatus.BLOCKED:
            matches = (
                review.verdict is ReviewVerdict.UNKNOWN
                and review.safe_to_create_pr is False
                and review.findings == ()
                and state.run.status is RunStatus.BLOCKED
                and state.graph.current_node in {"FINALIZE", "END"}
                and state.graph.pending_resume_node is None
            )
        elif analysis.verdict is DeliveryReviewVerdict.PASS:
            matches = (
                review.verdict is ReviewVerdict.PASS
                and review.safe_to_create_pr is True
                and review.findings == ()
                and (
                    (
                        state.run.status is RunStatus.RUNNING
                        and state.graph.current_node == "HUMAN_CHECKPOINT"
                        and state.graph.pending_resume_node == "CREATE_PR"
                    )
                    or (
                        state.graph.current_node in {"CREATE_PR", "FINALIZE", "END"}
                        and state.graph.pending_resume_node is None
                        and state.run.status
                        in {
                            RunStatus.RUNNING,
                            RunStatus.COMPLETED,
                            RunStatus.BLOCKED,
                            RunStatus.CANCELLED,
                            RunStatus.FAILED,
                        }
                    )
                )
            )
        else:
            matches = (
                review.verdict is ReviewVerdict.FAIL
                and review.safe_to_create_pr is False
                and review.findings == findings
                and state.run.status is RunStatus.FAILED
                and state.graph.current_node in {"FINALIZE", "END"}
                and state.graph.pending_resume_node is None
            )
        if not matches:
            raise DeliveryReviewStateMismatchError("delivery_review_state_mismatch")

    @staticmethod
    def _mechanical_analysis(findings: tuple[str, ...]) -> DeliveryReviewAnalysis:
        if not findings:
            raise DeliveryReviewEvidenceError("delivery_review_evidence_mismatch")
        return DeliveryReviewAnalysis(
            1,
            AgentAnalysisStatus.SUCCESS,
            DeliveryReviewVerdict.FAIL,
            "Deterministic delivery checks failed.",
            tuple(
                DeliveryReviewFinding(
                    DeliveryReviewFindingKind.DELIVERY_SCOPE_VIOLATION,
                    finding,
                    None,
                    (),
                    (),
                )
                for finding in findings
            ),
            None,
            None,
        )

    def _report(self, manifest, verdict, findings, reference):
        return DeliveryReviewReport(
            manifest.scope_id,
            manifest.target_baseline_head,
            manifest.final_head,
            manifest.final_tree_id,
            manifest.digest,
            verdict,
            verdict is ReviewVerdict.PASS,
            findings,
            reference,
        )


@dataclass(frozen=True, slots=True)
class DeliveryReviewNode:
    execution: DeliveryReviewExecution
    node_id: str = "DELIVERY_REVIEW"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            status, verdict, findings, reason_code = self.execution.review(
                state, context.node_attempt_id
            )
        except AgentResponseContractError as exc:
            self.execution.issue_code = "delivery_review_contract_invalid"
            return self._failure(
                context,
                NodeStatus.FAILED,
                "delivery_review_contract_invalid",
                str(exc),
                FailureCategory.CONTRACT,
            )
        except AgentMutationError as exc:
            self.execution.issue_code = "delivery_review_provider_mutated_repository"
            return self._failure(
                context,
                NodeStatus.FAILED,
                self.execution.issue_code,
                str(exc),
                FailureCategory.INFRASTRUCTURE,
            )
        except AgentError as exc:
            self.execution.issue_code = getattr(exc, "code", "delivery_review_provider_failed")
            return self._failure(
                context,
                NodeStatus.FAILED,
                self.execution.issue_code,
                str(exc),
                FailureCategory.INFRASTRUCTURE,
            )
        except WorkspaceError as exc:
            self.execution.issue_code = getattr(exc, "code", "delivery_review_failed")
            return self._failure(
                context,
                NodeStatus.FAILED,
                self.execution.issue_code,
                str(exc),
                FailureCategory.INFRASTRUCTURE,
            )
        if status is NodeStatus.BLOCKED:
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.BLOCKED,
                reason=ResultReason(
                    reason_code or "delivery_review_blocked", "delivery reviewer blocked"
                ),
            )
        assert verdict is not None
        operations = (
            PatchOperation.set("review.verdict", verdict),
            PatchOperation.set("review.safe_to_create_pr", verdict is ReviewVerdict.PASS),
            PatchOperation.set("review.findings", findings),
        )
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            state_patch=StatePatch(state.state_version, operations),
        )

    def _failure(self, context, status, code, message, category):
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            status,
            reason=ResultReason(code, message),
            failure_category=category,
        )
