"""Immutable, versioned graph-state snapshot and neutral nested models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .enums import (
    FailureCategory,
    ProgrammerRoute,
    RepairClassification,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    ValidationVerdict,
)
from .errors import ContractValidationError


def immutable_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Return a recursively immutable copy suitable for DTO metadata."""

    return MappingProxyType({key: _immutable_value(item) for key, item in (value or {}).items()})


def _immutable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return immutable_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_immutable_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RunState:
    """Engine-owned run identity and lifecycle status."""

    run_id: str
    status: RunStatus = RunStatus.RUNNING

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ContractValidationError("run_id is required")
        if not isinstance(self.status, RunStatus):
            raise ContractValidationError("run status must be a RunStatus")


@dataclass(frozen=True, slots=True)
class RepositoryState:
    """Opaque repository metadata; core does not inspect a repository."""

    identifier: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class GraphProgress:
    """Engine-owned graph cursor."""

    current_node: str = "START"
    previous_node: str | None = None
    transition_seq: int = 0
    pending_resume_node: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.current_node, str)
            or not self.current_node
            or type(self.transition_seq) is not int
            or self.transition_seq < 0
        ):
            raise ContractValidationError("invalid graph progress")
        if self.previous_node is not None and not isinstance(self.previous_node, str):
            raise ContractValidationError("previous_node must be a string or None")
        if self.pending_resume_node is not None and not isinstance(self.pending_resume_node, str):
            raise ContractValidationError("pending_resume_node must be a string or None")


@dataclass(frozen=True, slots=True)
class ProjectState:
    """Neutral project facts discovered by nodes."""

    name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class WorkHierarchyItem:
    """Opaque source hierarchy entry not interpreted by core."""

    level: str
    id: str
    title: str = ""
    status: str | None = None
    revision: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        if not self.level or not self.id:
            raise ContractValidationError("hierarchy level and id are required")
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class WorkItem:
    """The only required unit of work understood by core."""

    id: str
    title: str = ""
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        if not self.id:
            raise ContractValidationError("work item id is required")
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class DeliveryScope:
    """Optional neutral delivery boundary."""

    id: str | None = None
    allowed_paths: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class WorkState:
    """Source-neutral work selection state."""

    source: str | None = None
    hierarchy: tuple[WorkHierarchyItem, ...] = ()
    item: WorkItem | None = None
    completed_items: tuple[WorkItem, ...] = ()
    dependencies: tuple[str, ...] = ()
    delivery_scope: DeliveryScope | None = None
    available_items: tuple[WorkItem, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(item, WorkHierarchyItem) for item in self.hierarchy):
            raise ContractValidationError("work hierarchy contains an invalid item")
        if self.item is not None and not isinstance(self.item, WorkItem):
            raise ContractValidationError("work.item must be a WorkItem")
        if not all(isinstance(item, WorkItem) for item in self.completed_items):
            raise ContractValidationError("completed_items contains an invalid item")
        if not all(isinstance(item, str) for item in self.dependencies):
            raise ContractValidationError("dependencies must contain strings")
        if self.delivery_scope is not None and not isinstance(self.delivery_scope, DeliveryScope):
            raise ContractValidationError("delivery_scope must be a DeliveryScope")
        if not all(isinstance(item, WorkItem) for item in self.available_items):
            raise ContractValidationError("available_items contains an invalid item")


@dataclass(frozen=True, slots=True)
class TaskPackageState:
    """Neutral prepared context for an implementation node."""

    ready: bool = False
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class TextCollectionState:
    """Immutable collection of text statements."""

    items: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BaselineState:
    """Baseline facts captured before changes."""

    revision: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class ScopeState:
    """Mutable-by-authorized-node scope until the engine locks it."""

    included: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    locked: bool = False

    def __post_init__(self) -> None:
        if type(self.locked) is not bool:
            raise ContractValidationError("scope.locked must be boolean")
        if not all(isinstance(item, str) for item in (*self.included, *self.excluded)):
            raise ContractValidationError("scope paths must be strings")


@dataclass(frozen=True, slots=True)
class RiskState:
    """Assessed risk and engine-selected programmer route."""

    level: RiskLevel | None = None
    programmer_route: ProgrammerRoute | None = None

    def __post_init__(self) -> None:
        if self.level is not None and not isinstance(self.level, RiskLevel):
            raise ContractValidationError("risk level must be a RiskLevel")
        if self.programmer_route is not None and not isinstance(
            self.programmer_route, ProgrammerRoute
        ):
            raise ContractValidationError("programmer route must be a ProgrammerRoute")


@dataclass(frozen=True, slots=True)
class ChangesState:
    """Files and identifiers reported by an implementation node."""

    agent_reported_files: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    count: int = 0

    def __post_init__(self) -> None:
        if type(self.count) is not int or self.count < 0:
            raise ContractValidationError("changes.count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ValidationState:
    """Latest validation verdict and typed checks."""

    verdict: ValidationVerdict = ValidationVerdict.UNKNOWN
    checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ValidationVerdict):
            raise ContractValidationError("validation verdict must be a ValidationVerdict")


@dataclass(frozen=True, slots=True)
class ReviewState:
    """Latest review verdict and close-safety decision."""

    verdict: ReviewVerdict = ReviewVerdict.UNKNOWN
    safe_to_close: bool = False
    findings: tuple[str, ...] = ()
    safe_to_create_pr: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ReviewVerdict):
            raise ContractValidationError("review verdict must be a ReviewVerdict")
        if type(self.safe_to_close) is not bool:
            raise ContractValidationError("safe_to_close must be boolean")
        if type(self.safe_to_create_pr) is not bool:
            raise ContractValidationError("safe_to_create_pr must be boolean")


@dataclass(frozen=True, slots=True)
class RepairRecord:
    """One immutable repair entry."""

    id: str
    classification: RepairClassification


@dataclass(frozen=True, slots=True)
class RepairState:
    """Engine-owned count/limit plus classifier-owned routing data."""

    count: int = 0
    max_cycles: int = 0
    classification: RepairClassification | None = None
    history: tuple[RepairRecord, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.count) is not int
            or type(self.max_cycles) is not int
            or self.count < 0
            or self.max_cycles < 0
            or self.count > self.max_cycles
        ):
            raise ContractValidationError("invalid repair counters")
        if self.classification is not None and not isinstance(
            self.classification, RepairClassification
        ):
            raise ContractValidationError("invalid repair classification")
        if not all(isinstance(item, RepairRecord) for item in self.history):
            raise ContractValidationError("repair history contains an invalid record")


@dataclass(frozen=True, slots=True)
class OperationCollectionState:
    """Engine-owned future operation receipts."""

    records: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FailureState:
    """Latest typed failure summary."""

    category: FailureCategory | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        if self.category is not None and not isinstance(self.category, FailureCategory):
            raise ContractValidationError("failure category must be typed")


@dataclass(frozen=True, slots=True)
class CancellationState:
    """Cancellation information."""

    requested: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.requested) is not bool:
            raise ContractValidationError("cancellation.requested must be boolean")


@dataclass(frozen=True, slots=True)
class GraphState:
    """Canonical immutable v1 state snapshot."""

    run: RunState
    schema_version: int = 1
    state_version: int = 0
    repository: RepositoryState = field(default_factory=RepositoryState)
    graph: GraphProgress = field(default_factory=GraphProgress)
    project: ProjectState = field(default_factory=ProjectState)
    work: WorkState = field(default_factory=WorkState)
    task_package: TaskPackageState = field(default_factory=TaskPackageState)
    requirements: TextCollectionState = field(default_factory=TextCollectionState)
    acceptance_criteria: TextCollectionState = field(default_factory=TextCollectionState)
    architecture_invariants: TextCollectionState = field(default_factory=TextCollectionState)
    baseline: BaselineState = field(default_factory=BaselineState)
    scope: ScopeState = field(default_factory=ScopeState)
    risk: RiskState = field(default_factory=RiskState)
    changes: ChangesState = field(default_factory=ChangesState)
    validation: ValidationState = field(default_factory=ValidationState)
    review: ReviewState = field(default_factory=ReviewState)
    repair: RepairState = field(default_factory=RepairState)
    checkpoints: OperationCollectionState = field(default_factory=OperationCollectionState)
    commits: OperationCollectionState = field(default_factory=OperationCollectionState)
    push: OperationCollectionState = field(default_factory=OperationCollectionState)
    pull_request: OperationCollectionState = field(default_factory=OperationCollectionState)
    failure: FailureState = field(default_factory=FailureState)
    cancellation: CancellationState = field(default_factory=CancellationState)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractValidationError("only GraphState schema version 1 is supported")
        if type(self.state_version) is not int or self.state_version < 0:
            raise ContractValidationError("state_version cannot be negative")
        nested_types = {
            "run": RunState,
            "repository": RepositoryState,
            "graph": GraphProgress,
            "project": ProjectState,
            "work": WorkState,
            "task_package": TaskPackageState,
            "requirements": TextCollectionState,
            "acceptance_criteria": TextCollectionState,
            "architecture_invariants": TextCollectionState,
            "baseline": BaselineState,
            "scope": ScopeState,
            "risk": RiskState,
            "changes": ChangesState,
            "validation": ValidationState,
            "review": ReviewState,
            "repair": RepairState,
            "checkpoints": OperationCollectionState,
            "commits": OperationCollectionState,
            "push": OperationCollectionState,
            "pull_request": OperationCollectionState,
            "failure": FailureState,
            "cancellation": CancellationState,
        }
        if invalid := [
            name
            for name, expected in nested_types.items()
            if not isinstance(getattr(self, name), expected)
        ]:
            raise ContractValidationError(f"invalid GraphState group: {invalid[0]}")

    @classmethod
    def initial(cls, run_id: str, *, max_repair_cycles: int = 0) -> GraphState:
        """Create the initial snapshot for an in-memory run."""

        return cls(run=RunState(run_id), repair=RepairState(max_cycles=max_repair_cycles))


GraphStateSnapshot = GraphState
