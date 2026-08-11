"""Public contracts for the deterministic Agent Graph Engine core."""

from .edges import Edge, RetryPolicy, Transition
from .engine import GraphEngine
from .enums import (
    CheckpointOutcome,
    CommitMode,
    FailureCategory,
    NodeStatus,
    NodeType,
    OperationType,
    ProgrammerRoute,
    RepairClassification,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    ValidationVerdict,
)
from .errors import (
    AgentGraphError,
    AmbiguousTransitionError,
    ContractValidationError,
    GraphDefinitionError,
    GraphTransitionError,
    InvalidStatePatchError,
    NoValidTransitionError,
    StaleStatePatchError,
    StatePatchError,
    UnauthorizedStatePatchError,
)
from .graph import GraphDefinition
from .guards import Guard, GuardResult
from .node import Node, NodeContext, NodeDefinition
from .patches import PatchOperation, PatchOwnershipPolicy, StatePatch, StatePatchApplier
from .policy import PolicySnapshot
from .result import CheckpointRequest, Evidence, ExternalEffect, NodeResult, ResultReason
from .state import (
    DeliveryScope,
    GraphState,
    GraphStateSnapshot,
    WorkHierarchyItem,
    WorkItem,
    WorkState,
)
from .v1_graph import CANONICAL_V1_GRAPH, V1_NODE_IDS, canonical_v1_graph

__all__ = [
    "CANONICAL_V1_GRAPH",
    "V1_NODE_IDS",
    "AgentGraphError",
    "AmbiguousTransitionError",
    "CheckpointOutcome",
    "CheckpointRequest",
    "CommitMode",
    "ContractValidationError",
    "DeliveryScope",
    "Edge",
    "Evidence",
    "ExternalEffect",
    "FailureCategory",
    "GraphDefinition",
    "GraphDefinitionError",
    "GraphEngine",
    "GraphState",
    "GraphStateSnapshot",
    "GraphTransitionError",
    "Guard",
    "GuardResult",
    "InvalidStatePatchError",
    "NoValidTransitionError",
    "Node",
    "NodeContext",
    "NodeDefinition",
    "NodeResult",
    "NodeStatus",
    "NodeType",
    "OperationType",
    "PatchOperation",
    "PatchOwnershipPolicy",
    "PolicySnapshot",
    "ProgrammerRoute",
    "RepairClassification",
    "ResultReason",
    "RetryPolicy",
    "ReviewVerdict",
    "RiskLevel",
    "RunStatus",
    "StaleStatePatchError",
    "StatePatch",
    "StatePatchApplier",
    "StatePatchError",
    "Transition",
    "UnauthorizedStatePatchError",
    "ValidationVerdict",
    "WorkHierarchyItem",
    "WorkItem",
    "WorkState",
    "canonical_v1_graph",
]
