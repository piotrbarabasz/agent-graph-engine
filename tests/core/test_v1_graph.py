from dataclasses import replace

import pytest

from agentgraph.core import (
    CANONICAL_V1_GRAPH,
    CheckpointOutcome,
    FailureCategory,
    GraphEngine,
    NodeResult,
    NodeStatus,
    PatchOperation,
    PolicySnapshot,
    ProgrammerRoute,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    StatePatch,
    ValidationVerdict,
    WorkItem,
)
from agentgraph.core.state import GraphProgress, WorkState
from agentgraph.core.v1_graph import V1_NODE_IDS


def at(engine: GraphEngine, node_id: str, **changes: object):
    state = engine.initial_state("r")
    state = replace(state, graph=GraphProgress(current_node=node_id), **changes)
    return state


def success(node_id: str, state, *ops: PatchOperation, outcome=None) -> NodeResult:
    patch = StatePatch(state.state_version, ops) if ops else None
    return NodeResult(
        node_id,
        "a",
        NodeStatus.SUCCEEDED,
        state_patch=patch,
        checkpoint_outcome=outcome,
    )


def failure(node_id: str, category: FailureCategory, status=NodeStatus.FAILED) -> NodeResult:
    from agentgraph.core import ResultReason

    return NodeResult(
        node_id,
        "a",
        status,
        reason=ResultReason("failed", "typed failure") if status is NodeStatus.FAILED else None,
        failure_category=category if status is NodeStatus.FAILED else None,
    )


def test_canonical_node_ids_and_delivery_review_name() -> None:
    assert tuple(node.id for node in CANONICAL_V1_GRAPH.nodes) == V1_NODE_IDS
    assert "DELIVERY_REVIEW" in V1_NODE_IDS
    assert "EPIC_REVIEW" not in V1_NODE_IDS


@pytest.mark.parametrize(
    ("level", "target", "route"),
    [
        (RiskLevel.LOW, "IMPLEMENT", ProgrammerRoute.FAST),
        (RiskLevel.MEDIUM, "IMPLEMENT", ProgrammerRoute.FAST),
        (RiskLevel.HIGH, "IMPLEMENT", ProgrammerRoute.HIGH),
        (RiskLevel.CRITICAL, "HUMAN_CHECKPOINT", None),
    ],
)
def test_risk_routing(level: RiskLevel, target: str, route: ProgrammerRoute | None) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "ASSESS_RISK")
    state, transition = engine.apply_result(
        state,
        success("ASSESS_RISK", state, PatchOperation.set("risk.level", level)),
    )
    assert transition.to_node == target
    assert state.risk.programmer_route is route
    if level is RiskLevel.CRITICAL:
        assert state.graph.pending_resume_node == "IMPLEMENT"
        state, _ = engine.apply_result(
            state,
            success("HUMAN_CHECKPOINT", state, outcome=CheckpointOutcome.APPROVED),
        )
        assert state.graph.current_node == "IMPLEMENT"
        assert state.risk.programmer_route is ProgrammerRoute.HIGH


def test_critical_checkpoint_rejection_never_reaches_implement() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "ASSESS_RISK")
    state, _ = engine.apply_result(
        state,
        success("ASSESS_RISK", state, PatchOperation.set("risk.level", RiskLevel.CRITICAL)),
    )
    state, transition = engine.apply_result(
        state,
        success("HUMAN_CHECKPOINT", state, outcome=CheckpointOutcome.REJECTED),
    )
    assert transition.to_node == "FINALIZE"
    assert state.run.status is RunStatus.BLOCKED


@pytest.mark.parametrize(
    ("result_factory", "target", "status"),
    [
        (
            lambda state: success(
                "VALIDATE", state, PatchOperation.set("validation.verdict", ValidationVerdict.PASS)
            ),
            "REVIEW",
            RunStatus.RUNNING,
        ),
        (
            lambda state: failure("VALIDATE", FailureCategory.IMPLEMENTATION),
            "CLASSIFY_FAILURE",
            RunStatus.RUNNING,
        ),
        (
            lambda state: failure("VALIDATE", FailureCategory.INFRASTRUCTURE),
            "FINALIZE",
            RunStatus.FAILED,
        ),
        (
            lambda state: failure("VALIDATE", FailureCategory.ENVIRONMENT),
            "FINALIZE",
            RunStatus.FAILED,
        ),
        (
            lambda state: NodeResult("VALIDATE", "a", NodeStatus.TIMED_OUT),
            "FINALIZE",
            RunStatus.FAILED,
        ),
        (
            lambda state: NodeResult("VALIDATE", "a", NodeStatus.CANCELLED),
            "FINALIZE",
            RunStatus.CANCELLED,
        ),
    ],
)
def test_validation_routes(result_factory, target: str, status: RunStatus) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "VALIDATE")
    state, transition = engine.apply_result(state, result_factory(state))
    assert transition.to_node == target
    assert state.run.status is status


@pytest.mark.parametrize(
    ("result_factory", "target", "status"),
    [
        (
            lambda state: success(
                "REVIEW",
                state,
                PatchOperation.set("review.verdict", ReviewVerdict.PASS),
                PatchOperation.set("review.safe_to_close", True),
            ),
            "CLOSE_TASK",
            RunStatus.RUNNING,
        ),
        (
            lambda state: success(
                "REVIEW", state, PatchOperation.set("review.verdict", ReviewVerdict.FAIL)
            ),
            "CLASSIFY_FAILURE",
            RunStatus.RUNNING,
        ),
        (
            lambda state: success(
                "REVIEW", state, PatchOperation.set("review.verdict", ReviewVerdict.PASS)
            ),
            "FINALIZE",
            RunStatus.FAILED,
        ),
        (lambda state: failure("REVIEW", FailureCategory.CONTRACT), "FINALIZE", RunStatus.FAILED),
    ],
)
def test_review_routes(result_factory, target: str, status: RunStatus) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "REVIEW")
    state, transition = engine.apply_result(state, result_factory(state))
    assert transition.to_node == target
    assert state.run.status is status


@pytest.mark.parametrize(
    ("work", "limit", "target", "status"),
    [
        (WorkState(available_items=(WorkItem("B"),)), 3, "SELECT_WORK", RunStatus.RUNNING),
        (WorkState(), 3, "DELIVERY_REVIEW", RunStatus.RUNNING),
        (WorkState(completed_items=(WorkItem("A"),)), 1, "FINALIZE", RunStatus.PAUSED),
    ],
)
def test_more_work_routes(work: WorkState, limit: int, target: str, status: RunStatus) -> None:
    engine = GraphEngine(
        CANONICAL_V1_GRAPH,
        PolicySnapshot(max_work_items_per_run=limit),
    )
    state = at(engine, "MORE_WORK", work=work)
    state, transition = engine.apply_result(state, success("MORE_WORK", state))
    assert transition.to_node == target
    assert state.run.status is status
