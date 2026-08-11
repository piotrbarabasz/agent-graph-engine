from dataclasses import replace

import pytest

from agentgraph.core import (
    CANONICAL_V1_GRAPH,
    FailureCategory,
    GraphEngine,
    NodeResult,
    NodeStatus,
    PatchOperation,
    PolicySnapshot,
    RepairClassification,
    ResultReason,
    ReviewVerdict,
    RunStatus,
    StatePatch,
    ValidationVerdict,
)
from agentgraph.core.state import GraphProgress


def at(engine: GraphEngine, node_id: str):
    return replace(engine.initial_state("repair"), graph=GraphProgress(current_node=node_id))


def succeeded(node_id: str, state, *operations: PatchOperation) -> NodeResult:
    return NodeResult(
        node_id,
        "a",
        NodeStatus.SUCCEEDED,
        state_patch=StatePatch(state.state_version, operations) if operations else None,
    )


def failed(node_id: str, category: FailureCategory) -> NodeResult:
    return NodeResult(
        node_id,
        "a",
        NodeStatus.FAILED,
        reason=ResultReason("failure", "repairable failure"),
        failure_category=category,
    )


@pytest.mark.parametrize("maximum", [0, 1, 2])
def test_exact_number_of_repair_entries_is_enforced(maximum: int) -> None:
    engine = GraphEngine(
        CANONICAL_V1_GRAPH,
        PolicySnapshot(max_repair_cycles=maximum),
    )
    state = at(engine, "CLASSIFY_FAILURE")

    for expected_count in range(1, maximum + 1):
        state, transition = engine.apply_result(
            state,
            succeeded(
                "CLASSIFY_FAILURE",
                state,
                PatchOperation.set(
                    "repair.classification",
                    RepairClassification.PROGRAMMER,
                ),
            ),
        )
        assert transition.to_node == "PROGRAMMER_REPAIR"
        assert state.repair.count == expected_count
        state, _ = engine.apply_result(state, succeeded("PROGRAMMER_REPAIR", state))
        state, _ = engine.apply_result(
            state,
            failed("VALIDATE", FailureCategory.IMPLEMENTATION),
        )
        assert state.graph.current_node == "CLASSIFY_FAILURE"

    state, transition = engine.apply_result(
        state,
        succeeded(
            "CLASSIFY_FAILURE",
            state,
            PatchOperation.set("repair.classification", RepairClassification.PROGRAMMER),
        ),
    )
    assert transition.to_node == "FINALIZE"
    assert state.repair.count == maximum
    assert state.run.status is RunStatus.FAILED


def test_debugger_entry_increments_same_engine_counter_and_returns_to_validate() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=1))
    state = at(engine, "CLASSIFY_FAILURE")
    state, transition = engine.apply_result(
        state,
        succeeded(
            "CLASSIFY_FAILURE",
            state,
            PatchOperation.set("repair.classification", RepairClassification.DEBUGGER),
        ),
    )
    assert transition.to_node == "DEBUGGER"
    assert state.repair.count == 1

    state, transition = engine.apply_result(state, succeeded("DEBUGGER", state))
    assert transition.to_node == "VALIDATE"


def test_timeout_does_not_enter_repair() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    state = at(engine, "VALIDATE")
    state, transition = engine.apply_result(
        state,
        NodeResult("VALIDATE", "a", NodeStatus.TIMED_OUT),
    )
    assert transition.to_node == "FINALIZE"
    assert state.repair.count == 0


def test_review_failure_after_repair_uses_shared_limit() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=1))
    state = at(engine, "REVIEW")
    state, _ = engine.apply_result(
        state,
        succeeded(
            "REVIEW",
            state,
            PatchOperation.set("review.verdict", ReviewVerdict.FAIL),
        ),
    )
    state, _ = engine.apply_result(
        state,
        succeeded(
            "CLASSIFY_FAILURE",
            state,
            PatchOperation.set("repair.classification", RepairClassification.PROGRAMMER),
        ),
    )
    state, _ = engine.apply_result(state, succeeded("PROGRAMMER_REPAIR", state))
    state, _ = engine.apply_result(
        state,
        succeeded(
            "VALIDATE",
            state,
            PatchOperation.set("validation.verdict", ValidationVerdict.PASS),
        ),
    )
    state, _ = engine.apply_result(
        state,
        succeeded(
            "REVIEW",
            state,
            PatchOperation.set("review.verdict", ReviewVerdict.FAIL),
        ),
    )
    state, transition = engine.apply_result(
        state,
        succeeded(
            "CLASSIFY_FAILURE",
            state,
            PatchOperation.set("repair.classification", RepairClassification.DEBUGGER),
        ),
    )
    assert transition.to_node == "FINALIZE"
    assert state.repair.count == 1
