"""Strict result DTO returned by every node."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .enums import CheckpointOutcome, FailureCategory, NodeStatus
from .errors import ContractValidationError
from .patches import StatePatch
from .state import immutable_mapping


@dataclass(frozen=True, slots=True)
class ResultReason:
    """Stable code plus human-readable explanatory text."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not isinstance(self.message, str):
            raise ContractValidationError("reason code and message must be strings")
        if not self.code or not self.message:
            raise ContractValidationError("reason code and message are required")


@dataclass(frozen=True, slots=True)
class Evidence:
    """Neutral evidence descriptor; storage is outside M001."""

    kind: str
    reference: str
    metadata: Mapping[str, Any] = field(default_factory=immutable_mapping)

    def __post_init__(self) -> None:
        if not self.kind or not self.reference:
            raise ContractValidationError("evidence kind and reference are required")
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class ExternalEffect:
    """Neutral declaration of an observed external effect."""

    kind: str
    reference: str

    def __post_init__(self) -> None:
        if not self.kind or not self.reference:
            raise ContractValidationError("external effect kind and reference are required")


@dataclass(frozen=True, slots=True)
class CheckpointRequest:
    """Minimal checkpoint request modeled by core."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ContractValidationError("checkpoint code and message are required")


@dataclass(frozen=True, slots=True)
class NodeResult:
    """Validated, immutable node output consumed by GraphEngine."""

    node_id: str
    attempt_id: str
    status: NodeStatus
    schema_version: int = 1
    reason: ResultReason | None = None
    failure_category: FailureCategory | None = None
    state_patch: StatePatch | None = None
    evidence: tuple[Evidence, ...] = ()
    metrics: Mapping[str, int | float] = field(default_factory=immutable_mapping)
    external_effects: tuple[ExternalEffect, ...] = ()
    checkpoint_request: CheckpointRequest | None = None
    checkpoint_outcome: CheckpointOutcome | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractValidationError("only NodeResult schema version 1 is supported")
        if not self.node_id or not self.attempt_id:
            raise ContractValidationError("node_id and attempt_id are required")
        if not isinstance(self.status, NodeStatus):
            raise ContractValidationError("status must be a NodeStatus")
        object.__setattr__(self, "metrics", immutable_mapping(self.metrics))
        if self.failure_category is not None and not isinstance(
            self.failure_category, FailureCategory
        ):
            raise ContractValidationError("failure_category must be typed")
        if self.reason is not None and not isinstance(self.reason, ResultReason):
            raise ContractValidationError("reason must be a ResultReason")
        if self.state_patch is not None and not isinstance(self.state_patch, StatePatch):
            raise ContractValidationError("state_patch must be a StatePatch")
        if not all(isinstance(item, Evidence) for item in self.evidence):
            raise ContractValidationError("evidence contains an invalid value")
        if not all(isinstance(item, ExternalEffect) for item in self.external_effects):
            raise ContractValidationError("external_effects contains an invalid value")
        if not all(type(value) in {int, float} for value in self.metrics.values()):
            raise ContractValidationError("metrics values must be numeric")
        if self.checkpoint_request is not None and not isinstance(
            self.checkpoint_request, CheckpointRequest
        ):
            raise ContractValidationError("checkpoint_request must be typed")
        if self.checkpoint_outcome is not None and not isinstance(
            self.checkpoint_outcome, CheckpointOutcome
        ):
            raise ContractValidationError("checkpoint_outcome must be typed")
        if self.status is NodeStatus.FAILED:
            if self.reason is None or self.failure_category is None:
                raise ContractValidationError("FAILED requires reason and failure_category")
        elif self.failure_category is not None:
            raise ContractValidationError("only FAILED may carry failure_category")
        if self.status is NodeStatus.SUCCEEDED and self.reason is not None:
            raise ContractValidationError("SUCCEEDED cannot carry a failure reason")
        if self.status is NodeStatus.CHECKPOINT_REQUIRED and self.checkpoint_request is None:
            raise ContractValidationError("CHECKPOINT_REQUIRED requires checkpoint_request")
        if self.checkpoint_outcome is not None and self.status is not NodeStatus.SUCCEEDED:
            raise ContractValidationError("checkpoint_outcome requires SUCCEEDED status")
