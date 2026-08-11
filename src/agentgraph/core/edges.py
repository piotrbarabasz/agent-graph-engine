"""Typed edge conditions and transition DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .enums import (
    CheckpointOutcome,
    FailureCategory,
    NodeStatus,
    RepairClassification,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    ValidationVerdict,
)
from .guards import Guard, GuardResult
from .policy import PolicySnapshot
from .result import NodeResult
from .state import GraphState


class Condition(Protocol):
    """A pure, typed edge predicate."""

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        """Return whether the edge is eligible."""


@dataclass(frozen=True, slots=True)
class AlwaysCondition:
    """Condition matching every typed input."""

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del state, result, policy
        return True


@dataclass(frozen=True, slots=True)
class ResultStatusCondition:
    """Match one of a fixed set of node statuses."""

    statuses: frozenset[NodeStatus]

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del state, policy
        return result.status in self.statuses


@dataclass(frozen=True, slots=True)
class FailureCategoryCondition:
    """Match FAILED results with an allowed category."""

    categories: frozenset[FailureCategory]

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del state, policy
        return result.status is NodeStatus.FAILED and result.failure_category in self.categories


@dataclass(frozen=True, slots=True)
class RiskCondition:
    """Match the risk level already validated into state."""

    levels: frozenset[RiskLevel]

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result, policy
        return state.risk.level in self.levels


@dataclass(frozen=True, slots=True)
class ValidationCondition:
    """Match the latest validation verdict."""

    verdict: ValidationVerdict

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result, policy
        return state.validation.verdict is self.verdict


@dataclass(frozen=True, slots=True)
class ReviewCondition:
    """Match a review verdict and optional safe-to-close value."""

    verdict: ReviewVerdict
    safe_to_close: bool | None = None

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result, policy
        return state.review.verdict is self.verdict and (
            self.safe_to_close is None or state.review.safe_to_close is self.safe_to_close
        )


@dataclass(frozen=True, slots=True)
class RepairClassificationCondition:
    """Match a classifier-selected repair route."""

    classifications: frozenset[RepairClassification]

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result, policy
        return state.repair.classification in self.classifications


@dataclass(frozen=True, slots=True)
class RepairCapacityCondition:
    """Match whether another engine-counted repair entry is allowed."""

    available: bool

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result
        has_capacity = state.repair.count < policy.max_repair_cycles
        return has_capacity is self.available


@dataclass(frozen=True, slots=True)
class WorkCondition:
    """Match current or queued source-neutral work availability."""

    kind: str
    available: bool

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result, policy
        if self.kind == "current":
            present = state.work.item is not None
        elif self.kind == "more":
            present = bool(state.work.available_items)
        else:
            return False
        return present is self.available


@dataclass(frozen=True, slots=True)
class WorkLimitCondition:
    """Match whether the per-run completed-work limit has been reached."""

    reached: bool

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result
        is_reached = len(state.work.completed_items) >= policy.max_work_items_per_run
        return is_reached is self.reached


@dataclass(frozen=True, slots=True)
class CheckpointOutcomeCondition:
    """Match the synthetic M001 checkpoint decision."""

    outcomes: frozenset[CheckpointOutcome]

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del state, policy
        return result.checkpoint_outcome in self.outcomes


@dataclass(frozen=True, slots=True)
class PendingResumeCondition:
    """Match the engine-owned checkpoint resume target."""

    node_id: str

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        del result, policy
        return state.graph.pending_resume_node == self.node_id


@dataclass(frozen=True, slots=True)
class AllCondition:
    """Logical conjunction over explicit typed predicates."""

    conditions: tuple[Condition, ...]

    def matches(self, state: GraphState, result: NodeResult, policy: PolicySnapshot) -> bool:
        return all(item.matches(state, result, policy) for item in self.conditions)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Static retry metadata; M001 only executes graph repair transitions."""

    max_attempts: int = 0

    def __post_init__(self) -> None:
        if self.max_attempts < 0:
            raise ValueError("max_attempts cannot be negative")


@dataclass(frozen=True, slots=True)
class Edge:
    """A fully explicit directed graph edge."""

    id: str
    from_node: str
    to_node: str
    priority: int
    condition: Condition
    guards: tuple[Guard, ...] = ()
    retry_policy: RetryPolicy = RetryPolicy()
    checkpoint: bool = False
    terminal: bool = False
    final_status: RunStatus | None = None
    resume_node: str | None = None


@dataclass(frozen=True, slots=True)
class Transition:
    """A uniquely selected edge with evaluated guard evidence."""

    edge_id: str
    from_node: str
    to_node: str
    priority: int
    guard_results: tuple[GuardResult, ...] = ()
    checkpoint: bool = False
    terminal: bool = False
    final_status: RunStatus | None = None
    resume_node: str | None = None
