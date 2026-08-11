"""Node execution contracts without runtime integrations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .enums import NodeType
from .errors import ContractValidationError
from .policy import PolicySnapshot
from .result import NodeResult
from .state import GraphStateSnapshot


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Minimal immutable context supplied to a node invocation."""

    run_id: str
    node_attempt_id: str
    idempotency_key: str
    policy_snapshot: PolicySnapshot
    allowed_state_patch_paths: tuple[str, ...]
    deadline: datetime | None = None


class Node(Protocol):
    """Synchronous node contract used by the M001 in-memory engine."""

    node_id: str

    def run(self, state: GraphStateSnapshot, context: NodeContext) -> NodeResult:
        """Execute against an immutable snapshot and return a typed result."""


@dataclass(frozen=True, slots=True)
class NodeDefinition:
    """Static node metadata stored in a GraphDefinition."""

    id: str
    node_type: NodeType
    allowed_patch_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ContractValidationError("node definition id is required")
        if not isinstance(self.node_type, NodeType):
            raise ContractValidationError("node_type must be a NodeType")
