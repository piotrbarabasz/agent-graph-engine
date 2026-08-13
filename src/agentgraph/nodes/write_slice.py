"""Advisory analysis and controlled side-effect nodes for the write slice."""

from __future__ import annotations

from dataclasses import dataclass

from agentgraph.agents import (
    AgentAnalysisDriftError,
    AgentAnalysisStatus,
    AgentError,
    AgentEvidenceError,
    AgentMutationError,
    AgentResponseContractError,
    AgentResponseError,
    stable_union,
)
from agentgraph.agents.prompts import (
    build_explore_prompt,
    build_failure_classification_prompt,
    build_risk_prompt,
    build_task_package_prompt,
)
from agentgraph.core import (
    CheckpointRequest,
    Evidence,
    ExternalEffect,
    FailureCategory,
    NodeContext,
    NodeResult,
    NodeStatus,
    PatchOperation,
    RepairClassification,
    ResultReason,
    ReviewVerdict,
    RiskLevel,
    StatePatch,
    ValidationVerdict,
)
from agentgraph.core.state import GraphState, RepairRecord
from agentgraph.work import WorkRisk
from agentgraph.write.analysis import AgentExecution
from agentgraph.write.checkpoints import WriteCheckpointController
from agentgraph.write.errors import (
    ChangePathError,
    ChangeProviderBlockedError,
    PostCommitRecoveryRequired,
    SemanticReviewBlockedError,
    StaleFileError,
    WriteBaselineDriftError,
    WriteSliceError,
)
from agentgraph.write.models import WriteInputs
from agentgraph.write.workspace import WriteExecution


def _success(
    node_id: str, context: NodeContext, state: GraphState, *ops: PatchOperation
) -> NodeResult:
    patch = StatePatch(state.state_version, tuple(ops)) if ops else None
    return NodeResult(node_id, context.node_attempt_id, NodeStatus.SUCCEEDED, state_patch=patch)


def _blocked(
    node_id: str,
    context: NodeContext,
    code: str,
    message: str,
    evidence: tuple[Evidence, ...] = (),
) -> NodeResult:
    return NodeResult(
        node_id,
        context.node_attempt_id,
        NodeStatus.BLOCKED,
        reason=ResultReason(code, message),
        evidence=evidence,
    )


def _failed(
    node_id: str, context: NodeContext, code: str, message: str, category: FailureCategory
) -> NodeResult:
    return NodeResult(
        node_id,
        context.node_attempt_id,
        NodeStatus.FAILED,
        reason=ResultReason(code, message),
        failure_category=category,
    )


def _agent_failure(
    node_id: str, context: NodeContext, exc: Exception, execution: AgentExecution
) -> NodeResult:
    reason_code = getattr(exc, "reason_code", None)
    message = getattr(exc, "message", None)
    if isinstance(reason_code, str) and isinstance(message, str):
        execution.issue_code = reason_code
        return _blocked(node_id, context, reason_code, message)
    code = getattr(exc, "code", None) or "agent_invocation_failed"
    execution.issue_code = code
    if isinstance(exc, AgentAnalysisDriftError):
        return _blocked(
            node_id, context, "agent_analysis_baseline_drift", "pinned analysis inputs drifted"
        )
    if isinstance(exc, AgentEvidenceError):
        return _blocked(
            node_id,
            context,
            "agent_analysis_evidence_mismatch",
            "durable agent evidence does not match GraphState",
        )
    if isinstance(exc, AgentMutationError):
        return _failed(
            node_id,
            context,
            "agent_provider_mutated_repository",
            "read-only agent mutated the target repository",
            FailureCategory.INFRASTRUCTURE,
        )
    if isinstance(exc, AgentResponseError):
        return _failed(node_id, context, code, str(exc), FailureCategory.DESIGN)
    if isinstance(exc, AgentError):
        return _failed(node_id, context, code, str(exc), FailureCategory.INFRASTRUCTURE)
    return _failed(
        node_id,
        context,
        code,
        str(exc) or "read-only agent provider failed",
        FailureCategory.INFRASTRUCTURE,
    )


@dataclass(frozen=True, slots=True)
class ExploreNode:
    inputs: WriteInputs
    analysis: AgentExecution | None = None
    node_id: str = "EXPLORE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        package = self.inputs.package
        derived_requirements: tuple[str, ...] = ()
        derived_acceptance: tuple[str, ...] = ()
        evidence: tuple[Evidence, ...] = ()
        metadata = {"work_source_revision": self.inputs.source_revision}
        if self.analysis is not None:
            try:
                result = self.analysis.explore(
                    context.node_attempt_id, build_explore_prompt(self.inputs)
                )
            except Exception as exc:
                return _agent_failure(self.node_id, context, exc, self.analysis)
            value = result.value
            if value.status is AgentAnalysisStatus.BLOCKED:
                assert value.reason_code is not None and value.message is not None
                self.analysis.issue_code = value.reason_code
                return _blocked(
                    self.node_id,
                    context,
                    value.reason_code,
                    value.message,
                    (Evidence("agent_explore", result.evidence_reference),),
                )
            derived_requirements = value.derived_requirements
            derived_acceptance = value.derived_acceptance_criteria
            metadata.update(
                {
                    "agent_explore_evidence": result.evidence_reference,
                    "agent_explore_output_digest": result.response.output_digest,
                }
            )
            evidence = (Evidence("agent_explore", result.evidence_reference),)
        requirements = stable_union(
            tuple(value for value in (package.goal, *package.test_requirements) if value),
            derived_requirements,
        )
        acceptance = stable_union(package.acceptance_criteria, derived_acceptance)
        success = _success(
            self.node_id,
            context,
            state,
            PatchOperation.set("baseline.revision", self.inputs.baseline_head),
            PatchOperation.set("baseline.metadata", metadata),
            PatchOperation.set(
                "scope.included", tuple(path.path for path in self.inputs.expected_allowed_paths)
            ),
            PatchOperation.set("scope.excluded", ()),
            PatchOperation.set("requirements.items", requirements),
            PatchOperation.set("acceptance_criteria.items", acceptance),
        )
        return NodeResult(
            success.node_id,
            success.attempt_id,
            success.status,
            state_patch=success.state_patch,
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class BuildTaskPackageNode:
    inputs: WriteInputs
    analysis: AgentExecution | None = None
    node_id: str = "BUILD_TASK_PACKAGE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        package = self.inputs.package
        metadata: dict[str, object] = {
            "item_id": package.item_id,
            "scope_id": package.scope_id,
            "source_revision": self.inputs.source_revision,
            "capability_fingerprint": self.inputs.capability_fingerprint,
            "branch_hint": self.inputs.scope_branch,
            "base_branch_hint": self.inputs.base_branch,
        }
        evidence: tuple[Evidence, ...] = ()
        if self.analysis is not None:
            try:
                explore = self.analysis.explore_analysis or self.analysis.restore_explore(
                    state.baseline.metadata
                )
                result = self.analysis.build_task_package(
                    context.node_attempt_id,
                    build_task_package_prompt(self.inputs, explore),
                )
            except Exception as exc:
                return _agent_failure(self.node_id, context, exc, self.analysis)
            value = result.value
            if value.status is AgentAnalysisStatus.BLOCKED:
                assert value.reason_code is not None and value.message is not None
                self.analysis.issue_code = value.reason_code
                return _blocked(
                    self.node_id,
                    context,
                    value.reason_code,
                    value.message,
                    (Evidence("agent_task_package", result.evidence_reference),),
                )
            metadata.update(
                {
                    "objective": value.objective,
                    "implementation_steps": value.implementation_steps,
                    "recommended_change_paths": value.recommended_change_paths,
                    "supporting_read_paths": value.supporting_read_paths,
                    "validation_focus": value.validation_focus,
                    "assumptions": value.assumptions,
                    "agent_explore_evidence": state.baseline.metadata["agent_explore_evidence"],
                    "agent_explore_output_digest": state.baseline.metadata[
                        "agent_explore_output_digest"
                    ],
                    "agent_task_package_evidence": result.evidence_reference,
                    "agent_task_package_output_digest": result.response.output_digest,
                    "derived_constraints": explore.derived_constraints,
                    "relevant_files": explore.relevant_files,
                    "architecture_observations": explore.architecture_observations,
                }
            )
            evidence = (Evidence("agent_task_package", result.evidence_reference),)
        success = _success(
            self.node_id,
            context,
            state,
            PatchOperation.set("task_package.ready", True),
            PatchOperation.set("task_package.metadata", metadata),
        )
        return NodeResult(
            success.node_id,
            success.attempt_id,
            success.status,
            state_patch=success.state_patch,
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class AssessRiskNode:
    inputs: WriteInputs
    analysis: AgentExecution | None = None
    node_id: str = "ASSESS_RISK"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        if self.analysis is not None:
            try:
                explore = self.analysis.explore_analysis or self.analysis.restore_explore(
                    state.baseline.metadata
                )
                task_package = self.analysis.task_package or self.analysis.restore_task_package(
                    state.task_package.metadata
                )
                result, level = self.analysis.assess_risk(
                    context.node_attempt_id,
                    build_risk_prompt(self.inputs, explore, task_package),
                )
            except Exception as exc:
                return _agent_failure(self.node_id, context, exc, self.analysis)
            value = result.value
            if value.status is AgentAnalysisStatus.BLOCKED:
                assert value.reason_code is not None and value.message is not None
                self.analysis.issue_code = value.reason_code
                return _blocked(
                    self.node_id,
                    context,
                    value.reason_code,
                    value.message,
                    (Evidence("agent_risk", result.evidence_reference),),
                )
            assert level is not None
            if value.requests_human_checkpoint:
                level = RiskLevel.CRITICAL
            success = _success(
                self.node_id, context, state, PatchOperation.set("risk.level", level)
            )
            return NodeResult(
                success.node_id,
                success.attempt_id,
                success.status,
                state_patch=success.state_patch,
                evidence=(Evidence("agent_risk", result.evidence_reference),),
            )
        level = {
            WorkRisk.LOW: RiskLevel.LOW,
            WorkRisk.MEDIUM: RiskLevel.MEDIUM,
            WorkRisk.HIGH: RiskLevel.HIGH,
            WorkRisk.CRITICAL: RiskLevel.CRITICAL,
        }[self.inputs.package.risk]
        return _success(self.node_id, context, state, PatchOperation.set("risk.level", level))


@dataclass(frozen=True, slots=True)
class HumanCheckpointNode:
    checkpoints: WriteCheckpointController
    node_id: str = "HUMAN_CHECKPOINT"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            request, decision = self.checkpoints.decision(state)
        except WriteSliceError as exc:
            self.checkpoints.execution.issue_code = getattr(
                exc, "code", "checkpoint_evidence_invalid"
            )
            return _blocked(
                self.node_id,
                context,
                getattr(exc, "code", "checkpoint_evidence_invalid"),
                str(exc),
            )
        if decision is None:
            if self.checkpoints.expired(request):
                self.checkpoints.execution.issue_code = "checkpoint_expired"
                return _blocked(
                    self.node_id,
                    context,
                    "checkpoint_expired",
                    "The durable checkpoint expired before a decision was submitted.",
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
            evidence=(Evidence("human_checkpoint", self.checkpoints.decision_reference(request)),),
        )


@dataclass(frozen=True, slots=True)
class ImplementNode:
    execution: WriteExecution
    node_id: str = "IMPLEMENT"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            self.execution.prepare_implementation_analysis(state)
        except Exception as exc:
            return _agent_failure(self.node_id, context, exc, self.execution.analysis)
        try:
            applied = self.execution.implement(context.node_attempt_id)
        except ChangeProviderBlockedError as exc:
            return _blocked(self.node_id, context, exc.reason_code, exc.message)
        except WriteBaselineDriftError as exc:
            code = str(exc) or "write_baseline_drift"
            return _blocked(self.node_id, context, code, "pinned write inputs drifted")
        except WriteSliceError as exc:
            self.execution.issue_code = _error_code(exc)
            return _failed(
                self.node_id,
                context,
                _error_code(exc),
                str(exc),
                FailureCategory.IMPLEMENTATION,
            )
        except Exception:
            return _failed(
                self.node_id,
                context,
                "change_provider_failed",
                "change proposal or application failed",
                FailureCategory.IMPLEMENTATION,
            )
        manifest = self.execution.manifest
        assert manifest is not None
        paths = tuple(item.path for item in manifest.files)
        digest = applied.changeset_digest
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            state_patch=StatePatch(
                state.state_version,
                (
                    PatchOperation.set("changes.agent_reported_files", paths),
                    PatchOperation.set(
                        "changes.identifiers",
                        (
                            f"changeset:{digest}",
                            "operation_receipt:operations/implement-applied.json",
                            f"workspace_manifest:0:{manifest.digest}",
                        ),
                    ),
                    PatchOperation.set("changes.count", len(paths)),
                ),
            ),
            evidence=(Evidence("implement_applied", "operations/implement-applied.json"),),
            external_effects=(ExternalEffect("workspace_changes", digest),),
        )


@dataclass(frozen=True, slots=True)
class ValidateNode:
    execution: WriteExecution
    node_id: str = "VALIDATE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            cycle = state.repair.count
            passed = self.execution.validate(cycle)
        except WriteSliceError as exc:
            return _failed(
                self.node_id,
                context,
                _error_code(exc),
                str(exc),
                FailureCategory.INFRASTRUCTURE,
            )
        except Exception:
            return _failed(
                self.node_id,
                context,
                "validation_execution_failed",
                "validation infrastructure failed",
                FailureCategory.INFRASTRUCTURE,
            )
        verdict = ValidationVerdict.PASS if passed else ValidationVerdict.FAIL
        checks = tuple(receipt.command_id for receipt in self.execution.validation_receipts)
        operations = [
            PatchOperation.set("validation.verdict", verdict),
            PatchOperation.set("validation.checks", checks),
        ]
        if not passed:
            operations.extend(
                (
                    PatchOperation.set("failure.category", FailureCategory.VALIDATION),
                    PatchOperation.set("failure.code", "validation_failed"),
                )
            )
        else:
            operations.extend(
                (
                    PatchOperation.clear("failure.category"),
                    PatchOperation.clear("failure.code"),
                )
            )
        result = _success(self.node_id, context, state, *operations)
        return NodeResult(
            result.node_id,
            result.attempt_id,
            result.status,
            state_patch=result.state_patch,
            evidence=(Evidence("validation", _cycle_reference(cycle, "validation.json")),),
        )


@dataclass(frozen=True, slots=True)
class ReviewNode:
    execution: WriteExecution
    node_id: str = "REVIEW"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        cycle = state.repair.count
        try:
            passed, findings, failure_code = self.execution.review(
                state, cycle, context.node_attempt_id
            )
        except SemanticReviewBlockedError as exc:
            self.execution.issue_code = exc.reason_code
            return _blocked(
                self.node_id,
                context,
                exc.reason_code,
                exc.message,
                (Evidence("review", _cycle_reference(cycle, "review.json")),),
            )
        except AgentAnalysisDriftError as exc:
            return _agent_failure(self.node_id, context, exc, self.execution.analysis)
        except AgentResponseContractError as exc:
            self.execution.issue_code = exc.code
            return _failed(
                self.node_id,
                context,
                exc.code,
                str(exc),
                FailureCategory.CONTRACT,
            )
        except AgentMutationError as exc:
            self.execution.issue_code = exc.code
            return _failed(
                self.node_id,
                context,
                "agent_provider_mutated_repository",
                str(exc),
                FailureCategory.INFRASTRUCTURE,
            )
        except AgentError as exc:
            self.execution.issue_code = getattr(exc, "code", "agent_invocation_failed")
            return _failed(
                self.node_id,
                context,
                self.execution.issue_code,
                str(exc),
                FailureCategory.INFRASTRUCTURE,
            )
        except WriteSliceError as exc:
            self.execution.issue_code = _error_code(exc)
            category = (
                FailureCategory.CONTRACT
                if getattr(exc, "code", None) == "codex_response_invalid"
                else FailureCategory.INFRASTRUCTURE
            )
            return _failed(
                self.node_id,
                context,
                self.execution.issue_code,
                str(exc),
                category,
            )
        except Exception:
            return _failed(
                self.node_id,
                context,
                "review_execution_failed",
                "deterministic review infrastructure failed",
                FailureCategory.INFRASTRUCTURE,
            )
        operations = [
            PatchOperation.set(
                "review.verdict", ReviewVerdict.PASS if passed else ReviewVerdict.FAIL
            ),
            PatchOperation.set("review.safe_to_close", passed),
            PatchOperation.set("review.findings", findings),
        ]
        if not passed:
            operations.extend(
                (
                    PatchOperation.set("failure.category", FailureCategory.VALIDATION),
                    PatchOperation.set(
                        "failure.code", failure_code or "deterministic_review_failed"
                    ),
                )
            )
        else:
            operations.extend(
                (
                    PatchOperation.clear("failure.category"),
                    PatchOperation.clear("failure.code"),
                )
            )
        result = _success(self.node_id, context, state, *operations)
        return NodeResult(
            result.node_id,
            result.attempt_id,
            result.status,
            state_patch=result.state_patch,
            evidence=(Evidence("review", _cycle_reference(cycle, "review.json")),),
        )


@dataclass(frozen=True, slots=True)
class ClassifyFailureNode:
    execution: WriteExecution
    node_id: str = "CLASSIFY_FAILURE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        if state.repair.count >= state.repair.max_cycles:
            return _success(self.node_id, context, state)
        try:
            failure_context = self.execution.prepare_failure_context(state)
            category = failure_context.failure_category
            if category in {FailureCategory.IMPLEMENTATION, FailureCategory.DESIGN}:
                classification = RepairClassification.PROGRAMMER
                evidence: tuple[Evidence, ...] = ()
            elif category is FailureCategory.VALIDATION:
                result = self.execution.analysis.classify_failure(
                    context.node_attempt_id,
                    build_failure_classification_prompt(failure_context),
                    repository_root=self.execution.workspace,
                    expected_workspace_digest=failure_context.current_manifest_digest,
                    workspace_digest=self.execution.verify_current_manifest,
                )
                value = result.value
                if value.status is AgentAnalysisStatus.BLOCKED:
                    assert value.reason_code is not None and value.message is not None
                    self.execution.analysis.issue_code = value.reason_code
                    return _blocked(
                        self.node_id,
                        context,
                        value.reason_code,
                        value.message,
                        (Evidence("failure_classification", result.evidence_reference),),
                    )
                assert value.classification is not None
                classification = value.classification
                evidence = (Evidence("failure_classification", result.evidence_reference),)
            else:
                return _failed(
                    self.node_id,
                    context,
                    "failure_not_repairable_in_m009",
                    "failure category is outside the M009 repair policy",
                    FailureCategory.INFRASTRUCTURE,
                )
            record = RepairRecord(f"repair-{state.repair.count + 1:03d}", classification)
            if any(item.id == record.id for item in state.repair.history):
                raise ValueError("repair history collision")
            success = _success(
                self.node_id,
                context,
                state,
                PatchOperation.set("repair.classification", classification),
                PatchOperation.append_unique("repair.history", record),
            )
            return NodeResult(
                success.node_id,
                success.attempt_id,
                success.status,
                state_patch=success.state_patch,
                evidence=evidence,
            )
        except Exception as exc:
            return _agent_failure(self.node_id, context, exc, self.execution.analysis)


@dataclass(frozen=True, slots=True)
class RepairNode:
    execution: WriteExecution
    classification: RepairClassification
    node_id: str

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            applied, manifest = self.execution.repair(
                state, self.node_id, context.node_attempt_id, self.classification
            )
        except ChangeProviderBlockedError as exc:
            return _blocked(self.node_id, context, exc.reason_code, exc.message)
        except WriteSliceError as exc:
            self.execution.issue_code = _error_code(exc)
            return _failed(
                self.node_id,
                context,
                _error_code(exc),
                str(exc),
                FailureCategory.IMPLEMENTATION,
            )
        except Exception:
            return _failed(
                self.node_id,
                context,
                "repair_provider_failed",
                "repair proposal or application failed",
                FailureCategory.IMPLEMENTATION,
            )
        cycle = state.repair.count
        identifiers = (
            *state.changes.identifiers,
            f"repair_changeset:{cycle}:{applied.changeset_digest}",
            f"workspace_manifest:{cycle}:{manifest.digest}",
        )
        paths = tuple(item.path for item in manifest.files)
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            state_patch=StatePatch(
                state.state_version,
                (
                    PatchOperation.set("changes.agent_reported_files", paths),
                    PatchOperation.set("changes.identifiers", identifiers),
                    PatchOperation.set("changes.count", len(paths)),
                ),
            ),
            evidence=(Evidence("repair_applied", _cycle_reference(cycle, "applied.json")),),
            external_effects=(ExternalEffect("workspace_changes", manifest.digest),),
        )


@dataclass(frozen=True, slots=True)
class CloseTaskNode:
    execution: WriteExecution
    node_id: str = "CLOSE_TASK"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            commit = self.execution.commit()
        except PostCommitRecoveryRequired:
            raise
        except WriteBaselineDriftError as exc:
            return _blocked(
                self.node_id,
                context,
                str(exc) or "write_baseline_drift_before_commit",
                "pinned inputs drifted before commit",
            )
        except WriteSliceError as exc:
            return _failed(
                self.node_id,
                context,
                _error_code(exc),
                str(exc),
                FailureCategory.INFRASTRUCTURE,
            )
        except Exception:
            return _failed(
                self.node_id,
                context,
                "commit_execution_failed",
                "local commit infrastructure failed",
                FailureCategory.INFRASTRUCTURE,
            )
        item = state.work.item
        assert item is not None
        completed = (*state.work.completed_items, item)
        available = tuple(
            candidate for candidate in state.work.available_items if candidate.id != item.id
        )
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            state_patch=StatePatch(
                state.state_version,
                (
                    PatchOperation.clear("work.item"),
                    PatchOperation.set("work.completed_items", completed),
                    PatchOperation.set("work.available_items", available),
                ),
            ),
            evidence=(Evidence("local_commit", "operations/commit.json"),),
            external_effects=(ExternalEffect("local_commit", commit),),
        )


@dataclass(frozen=True, slots=True)
class MoreWorkNode:
    node_id: str = "MORE_WORK"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        return _success(self.node_id, context, state)


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, ChangePathError):
        return "out_of_scope_change" if str(exc) == "out_of_scope_change" else "unsafe_change_path"
    if isinstance(exc, StaleFileError):
        return "stale_file"
    value = str(exc)
    if value and " " not in value and value.replace("_", "").isalnum():
        return value
    return type(exc).__name__.replace("Error", "").lower() or "write_slice_error"


def _cycle_reference(cycle: int, name: str) -> str:
    return f"operations/{name}" if cycle == 0 else f"operations/repairs/{cycle:03d}/{name}"
