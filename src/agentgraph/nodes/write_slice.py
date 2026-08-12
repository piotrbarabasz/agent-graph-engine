"""Advisory analysis and controlled side-effect nodes for the write slice."""

from __future__ import annotations

from dataclasses import dataclass

from agentgraph.agents import (
    AgentAnalysisDriftError,
    AgentAnalysisStatus,
    AgentError,
    AgentEvidenceError,
    AgentMutationError,
    AgentResponseError,
    stable_union,
)
from agentgraph.agents.prompts import (
    build_explore_prompt,
    build_risk_prompt,
    build_task_package_prompt,
)
from agentgraph.core import (
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
from agentgraph.core.state import GraphState
from agentgraph.work import WorkRisk
from agentgraph.write.analysis import AgentExecution
from agentgraph.write.errors import (
    ChangePathError,
    ChangeProviderBlockedError,
    PostCommitRecoveryRequired,
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
            if level is RiskLevel.CRITICAL or value.requests_human_checkpoint:
                self.analysis.issue_code = "human_checkpoint_required_not_supported_in_m008"
                return _blocked(
                    self.node_id,
                    context,
                    "human_checkpoint_required_not_supported_in_m008",
                    "M008 does not implement human checkpoint approval",
                    (Evidence("agent_risk", result.evidence_reference),),
                )
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
        if self.inputs.package.risk is WorkRisk.CRITICAL:
            return _blocked(
                self.node_id,
                context,
                "critical_risk_not_supported_in_m006",
                "M006 does not execute critical-risk work",
            )
        level = {
            WorkRisk.LOW: RiskLevel.LOW,
            WorkRisk.MEDIUM: RiskLevel.MEDIUM,
            WorkRisk.HIGH: RiskLevel.HIGH,
        }[self.inputs.package.risk]
        return _success(self.node_id, context, state, PatchOperation.set("risk.level", level))


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
            applied = self.execution.implement()
        except ChangeProviderBlockedError as exc:
            return _blocked(self.node_id, context, exc.reason_code, exc.message)
        except WriteBaselineDriftError as exc:
            code = str(exc) or "write_baseline_drift"
            return _blocked(self.node_id, context, code, "pinned write inputs drifted")
        except WriteSliceError as exc:
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
        paths = tuple(item.path for item in applied.files)
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
            passed = self.execution.validate()
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
        result = _success(self.node_id, context, state, *operations)
        return NodeResult(
            result.node_id,
            result.attempt_id,
            result.status,
            state_patch=result.state_patch,
            evidence=(Evidence("validation", "operations/validation.json"),),
        )


@dataclass(frozen=True, slots=True)
class ReviewNode:
    execution: WriteExecution
    node_id: str = "REVIEW"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            passed, findings = self.execution.review()
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
                    PatchOperation.set("failure.code", "deterministic_review_failed"),
                )
            )
        result = _success(self.node_id, context, state, *operations)
        return NodeResult(
            result.node_id,
            result.attempt_id,
            result.status,
            state_patch=result.state_patch,
            evidence=(Evidence("review", "operations/review.json"),),
        )


@dataclass(frozen=True, slots=True)
class ClassifyFailureNode:
    node_id: str = "CLASSIFY_FAILURE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        classification = (
            RepairClassification.DEBUGGER
            if state.failure.category is FailureCategory.VALIDATION
            else RepairClassification.PROGRAMMER
        )
        return _success(
            self.node_id,
            context,
            state,
            PatchOperation.set("repair.classification", classification),
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
