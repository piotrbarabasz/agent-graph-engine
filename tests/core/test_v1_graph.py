from dataclasses import replace

import pytest

from agentgraph.core import (
    CANONICAL_V1_GRAPH,
    CheckpointOutcome,
    FailureCategory,
    GraphEngine,
    NodeResult,
    NodeStatus,
    NodeType,
    PatchOperation,
    PolicySnapshot,
    ProgrammerRoute,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    StatePatch,
    UnauthorizedStatePatchError,
    ValidationVerdict,
    WorkItem,
)
from agentgraph.core.state import GraphProgress, ReviewState, WorkState
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
    ("node_id", "node_type"),
    [
        ("START", NodeType.DETERMINISTIC),
        ("DISCOVER_PROJECT", NodeType.DETERMINISTIC),
        ("PREFLIGHT", NodeType.DETERMINISTIC),
        ("SELECT_WORK", NodeType.DETERMINISTIC),
        ("EXPLORE", NodeType.LLM_READ_ONLY),
        ("BUILD_TASK_PACKAGE", NodeType.LLM_READ_ONLY),
        ("ASSESS_RISK", NodeType.LLM_READ_ONLY),
        ("HUMAN_CHECKPOINT", NodeType.HUMAN_CHECKPOINT),
        ("IMPLEMENT", NodeType.LLM_WRITE),
        ("VALIDATE", NodeType.DETERMINISTIC),
        ("REVIEW", NodeType.LLM_READ_ONLY),
        ("CLASSIFY_FAILURE", NodeType.LLM_READ_ONLY),
        ("PROGRAMMER_REPAIR", NodeType.LLM_WRITE),
        ("DEBUGGER", NodeType.LLM_WRITE),
        ("CLOSE_TASK", NodeType.EXTERNAL_OPERATION),
        ("MORE_WORK", NodeType.DETERMINISTIC),
        ("DELIVERY_REVIEW", NodeType.LLM_READ_ONLY),
        ("CREATE_PR", NodeType.EXTERNAL_OPERATION),
        ("FINALIZE", NodeType.DETERMINISTIC),
        ("END", NodeType.DETERMINISTIC),
    ],
)
def test_canonical_node_type_mapping(node_id: str, node_type: NodeType) -> None:
    assert CANONICAL_V1_GRAPH.node(node_id).node_type is node_type


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


def test_completed_work_without_current_item_enters_delivery_review() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(
        engine,
        "SELECT_WORK",
        work=WorkState(completed_items=(WorkItem("ABC-123"),)),
    )

    state, transition = engine.apply_result(state, success("SELECT_WORK", state))

    assert transition.to_node == "DELIVERY_REVIEW"
    assert state.run.status is RunStatus.RUNNING


@pytest.mark.parametrize(
    ("verdict", "safe_to_create_pr", "target", "status"),
    [
        (ReviewVerdict.PASS, True, "HUMAN_CHECKPOINT", RunStatus.RUNNING),
        (ReviewVerdict.PASS, False, "FINALIZE", RunStatus.FAILED),
        (ReviewVerdict.FAIL, False, "FINALIZE", RunStatus.FAILED),
    ],
)
def test_delivery_review_gate(
    verdict: ReviewVerdict,
    safe_to_create_pr: bool,
    target: str,
    status: RunStatus,
) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "DELIVERY_REVIEW")
    state, transition = engine.apply_result(
        state,
        success(
            "DELIVERY_REVIEW",
            state,
            PatchOperation.set("review.verdict", verdict),
            PatchOperation.set("review.safe_to_create_pr", safe_to_create_pr),
        ),
    )

    assert transition.to_node == target
    assert state.run.status is status
    if target == "HUMAN_CHECKPOINT":
        assert state.graph.pending_resume_node == "CREATE_PR"


def test_stale_task_review_does_not_authorize_delivery() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    stale_review = ReviewState(verdict=ReviewVerdict.PASS, safe_to_close=True)
    state = at(engine, "DELIVERY_REVIEW", review=stale_review)

    state, transition = engine.apply_result(state, success("DELIVERY_REVIEW", state))

    assert transition.to_node == "FINALIZE"
    assert state.run.status is RunStatus.FAILED


def test_task_review_cannot_patch_delivery_pr_gate() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "REVIEW")
    with pytest.raises(UnauthorizedStatePatchError):
        engine.apply_result(
            state,
            success(
                "REVIEW",
                state,
                PatchOperation.set("review.safe_to_create_pr", True),
            ),
        )


def test_finalize_has_only_edges_to_end() -> None:
    outgoing = CANONICAL_V1_GRAPH.outgoing("FINALIZE")
    assert outgoing
    assert all(edge.to_node == "END" and edge.terminal for edge in outgoing)


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (FailureCategory.INFRASTRUCTURE, RunStatus.BLOCKED),
        (FailureCategory.ENVIRONMENT, RunStatus.BLOCKED),
        (FailureCategory.EXTERNAL_SERVICE, RunStatus.BLOCKED),
        (FailureCategory.POLICY, RunStatus.FAILED),
        (FailureCategory.CONTRACT, RunStatus.FAILED),
        (FailureCategory.INTERNAL, RunStatus.FAILED),
    ],
)
def test_create_pr_failure_semantics(category: FailureCategory, status: RunStatus) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "CREATE_PR")
    state, transition = engine.apply_result(state, failure("CREATE_PR", category))
    assert transition.to_node == "FINALIZE"
    assert state.run.status is status


@pytest.mark.parametrize(
    ("node_status", "run_status"),
    [
        (NodeStatus.TIMED_OUT, RunStatus.BLOCKED),
        (NodeStatus.CANCELLED, RunStatus.CANCELLED),
    ],
)
def test_create_pr_non_failure_terminal_statuses(
    node_status: NodeStatus, run_status: RunStatus
) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = at(engine, "CREATE_PR")
    state, transition = engine.apply_result(
        state,
        NodeResult("CREATE_PR", "a", node_status),
    )
    assert transition.to_node == "FINALIZE"
    assert state.run.status is run_status


def test_default_work_item_limit_matches_frozen_policy() -> None:
    assert PolicySnapshot().max_work_items_per_run == 20
