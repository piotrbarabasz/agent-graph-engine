"""Deterministic bridges and controlled side-effect nodes for M006."""

from __future__ import annotations

from dataclasses import dataclass

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
from agentgraph.write.errors import (
    ChangePathError,
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


def _blocked(node_id: str, context: NodeContext, code: str, message: str) -> NodeResult:
    return NodeResult(
        node_id,
        context.node_attempt_id,
        NodeStatus.BLOCKED,
        reason=ResultReason(code, message),
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


@dataclass(frozen=True, slots=True)
class ExploreNode:
    inputs: WriteInputs
    node_id: str = "EXPLORE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        package = self.inputs.package
        requirements = tuple(value for value in (package.goal, *package.test_requirements) if value)
        return _success(
            self.node_id,
            context,
            state,
            PatchOperation.set("baseline.revision", self.inputs.baseline_head),
            PatchOperation.set(
                "baseline.metadata",
                {"work_source_revision": self.inputs.source_revision},
            ),
            PatchOperation.set(
                "scope.included", tuple(path.path for path in self.inputs.expected_allowed_paths)
            ),
            PatchOperation.set("scope.excluded", ()),
            PatchOperation.set("requirements.items", requirements),
            PatchOperation.set("acceptance_criteria.items", package.acceptance_criteria),
        )


@dataclass(frozen=True, slots=True)
class BuildTaskPackageNode:
    inputs: WriteInputs
    node_id: str = "BUILD_TASK_PACKAGE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        package = self.inputs.package
        return _success(
            self.node_id,
            context,
            state,
            PatchOperation.set("task_package.ready", True),
            PatchOperation.set(
                "task_package.metadata",
                {
                    "item_id": package.item_id,
                    "scope_id": package.scope_id,
                    "source_revision": self.inputs.source_revision,
                    "capability_fingerprint": self.inputs.capability_fingerprint,
                    "branch_hint": self.inputs.scope_branch,
                    "base_branch_hint": self.inputs.base_branch,
                },
            ),
        )


@dataclass(frozen=True, slots=True)
class AssessRiskNode:
    inputs: WriteInputs
    node_id: str = "ASSESS_RISK"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
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
            applied = self.execution.implement()
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
    if isinstance(exc, ChangePathError):
        return "out_of_scope_change" if str(exc) == "out_of_scope_change" else "unsafe_change_path"
    if isinstance(exc, StaleFileError):
        return "stale_file"
    value = str(exc)
    if value and " " not in value and value.replace("_", "").isalnum():
        return value
    return type(exc).__name__.replace("Error", "").lower() or "write_slice_error"
