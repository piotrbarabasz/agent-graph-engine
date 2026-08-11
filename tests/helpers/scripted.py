"""Scripted nodes used for full in-memory graph tests."""

from __future__ import annotations

from dataclasses import dataclass

from agentgraph.core import (
    CheckpointOutcome,
    FailureCategory,
    NodeContext,
    NodeResult,
    NodeStatus,
    PatchOperation,
    ResultReason,
    StatePatch,
)
from agentgraph.core.state import GraphState


@dataclass(frozen=True, slots=True)
class ResultTemplate:
    """Version-independent description of one scripted node result."""

    status: NodeStatus = NodeStatus.SUCCEEDED
    operations: tuple[PatchOperation, ...] = ()
    failure_category: FailureCategory | None = None
    checkpoint_outcome: CheckpointOutcome | None = None


class ScriptedNode:
    """Return templates in order while binding attempt and state versions."""

    def __init__(
        self,
        node_id: str,
        *templates: ResultTemplate,
        call_log: list[str] | None = None,
    ) -> None:
        self.node_id = node_id
        self.templates = templates or (ResultTemplate(),)
        self.calls = 0
        self.call_log = call_log

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            template = self.templates[self.calls]
        except IndexError as exc:
            raise AssertionError(f"unexpected extra call to {self.node_id}") from exc
        self.calls += 1
        if self.call_log is not None:
            self.call_log.append(self.node_id)
        reason = None
        if template.status is NodeStatus.FAILED:
            reason = ResultReason("scripted_failure", "scripted node failed")
        patch = (
            StatePatch(state.state_version, template.operations) if template.operations else None
        )
        return NodeResult(
            node_id=self.node_id,
            attempt_id=context.node_attempt_id,
            status=template.status,
            reason=reason,
            failure_category=template.failure_category,
            state_patch=patch,
            checkpoint_outcome=template.checkpoint_outcome,
        )
