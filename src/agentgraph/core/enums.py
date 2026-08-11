"""Enumerations used by the deterministic graph core."""

from __future__ import annotations

from enum import StrEnum


class NodeType(StrEnum):
    """A node's capability classification."""

    DETERMINISTIC = "deterministic"
    LLM_READ_ONLY = "llm_read_only"
    LLM_WRITE = "llm_write"
    HUMAN_CHECKPOINT = "human_checkpoint"
    EXTERNAL_OPERATION = "external_operation"


class NodeStatus(StrEnum):
    """The single outcome status returned by a node."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CHECKPOINT_REQUIRED = "checkpoint_required"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class FailureCategory(StrEnum):
    """Typed failure categories used by transition conditions."""

    IMPLEMENTATION = "implementation"
    DESIGN = "design"
    VALIDATION = "validation"
    POLICY = "policy"
    CONTRACT = "contract"
    INFRASTRUCTURE = "infrastructure"
    ENVIRONMENT = "environment"
    EXTERNAL_SERVICE = "external_service"
    INTERNAL = "internal"


class RunStatus(StrEnum):
    """Engine-owned lifecycle state for a run."""

    RUNNING = "running"
    COMPLETED = "completed"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    """Risk level produced by assessment."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ProgrammerRoute(StrEnum):
    """Engine-selected implementation route."""

    FAST = "fast"
    HIGH = "high"


class ValidationVerdict(StrEnum):
    """Validation outcome recorded in state."""

    UNKNOWN = "unknown"
    PASS = "pass"
    FAIL = "fail"


class ReviewVerdict(StrEnum):
    """Review outcome recorded in state."""

    UNKNOWN = "unknown"
    PASS = "pass"
    FAIL = "fail"


class RepairClassification(StrEnum):
    """Typed repair route selected by failure classification."""

    PROGRAMMER = "programmer"
    DEBUGGER = "debugger"


class CheckpointOutcome(StrEnum):
    """Synthetic checkpoint outcome used by the M001 core."""

    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CommitMode(StrEnum):
    """Future commit policy contract."""

    PER_WORK_ITEM = "per_work_item"
    DELIVERY = "delivery"
    DISABLED = "disabled"


class OperationType(StrEnum):
    """Supported state-patch operations."""

    SET = "set"
    APPEND_UNIQUE = "append_unique"
    INCREMENT = "increment"
    MERGE_BY_ID = "merge_by_id"
    CLEAR = "clear"
