from __future__ import annotations

from dataclasses import replace

import pytest

from agentgraph.core import (
    CANONICAL_V1_GRAPH,
    FailureCategory,
    GraphEngine,
    NodeResult,
    NodeStatus,
    NoValidTransitionError,
    PatchOperation,
    PolicySnapshot,
    ProgrammerRoute,
    RepairClassification,
    ResultReason,
    ReviewVerdict,
    RiskLevel,
    StatePatch,
    UnauthorizedStatePatchError,
    ValidationVerdict,
    WorkItem,
)
from agentgraph.core.state import (
    BaselineState,
    CancellationState,
    ChangesState,
    DeliveryScope,
    FailureState,
    GraphProgress,
    ProjectState,
    RepairRecord,
    RepairState,
    RepositoryState,
    ReviewState,
    RiskState,
    ScopeState,
    TaskPackageState,
    TextCollectionState,
    ValidationState,
    WorkState,
)


def success(node_id: str, state, *operations: PatchOperation) -> NodeResult:
    return NodeResult(
        node_id,
        "attempt",
        NodeStatus.SUCCEEDED,
        state_patch=StatePatch(state.state_version, operations) if operations else None,
    )


def failure(node_id: str) -> NodeResult:
    return NodeResult(
        node_id,
        "attempt",
        NodeStatus.FAILED,
        reason=ResultReason("implementation", "implementation failed"),
        failure_category=FailureCategory.IMPLEMENTATION,
    )


def dirty_more_work_state(engine: GraphEngine, *, has_more_work: bool = True):
    first = WorkItem("A")
    available = (WorkItem("B"),) if has_more_work else ()
    state = engine.initial_state("item-reset")
    return replace(
        state,
        repository=RepositoryState("repo"),
        graph=GraphProgress(current_node="MORE_WORK"),
        project=ProjectState("project"),
        work=WorkState(
            source="test",
            completed_items=(first,),
            available_items=available,
            delivery_scope=DeliveryScope("delivery", ("src",)),
        ),
        task_package=TaskPackageState(True, {"item": "A"}),
        requirements=TextCollectionState(("requirement A",)),
        acceptance_criteria=TextCollectionState(("criterion A",)),
        architecture_invariants=TextCollectionState(("preserve architecture",)),
        baseline=BaselineState("baseline-revision", {"preserve": True}),
        scope=ScopeState(("src/a.py",), ("src/b.py",), True),
        risk=RiskState(RiskLevel.HIGH, ProgrammerRoute.HIGH),
        changes=ChangesState(("src/a.py",), ("symbol",), 1),
        validation=ValidationState(ValidationVerdict.PASS, ("tests",)),
        review=ReviewState(
            verdict=ReviewVerdict.PASS,
            safe_to_close=True,
            findings=("task finding",),
            safe_to_create_pr=True,
        ),
        repair=RepairState(
            count=2,
            max_cycles=2,
            classification=RepairClassification.PROGRAMMER,
            history=(RepairRecord("repair-A", RepairClassification.PROGRAMMER),),
        ),
        failure=FailureState(FailureCategory.IMPLEMENTATION, "old-failure"),
        cancellation=CancellationState(True, "run-level cancellation fact"),
    )


def test_more_work_to_select_work_resets_item_scope_and_preserves_run_state() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    before = dirty_more_work_state(engine)

    state, transition = engine.apply_result(before, success("MORE_WORK", before))

    assert transition.to_node == "SELECT_WORK"
    assert state.task_package == TaskPackageState()
    assert state.requirements == TextCollectionState()
    assert state.acceptance_criteria == TextCollectionState()
    assert state.scope == ScopeState()
    assert state.risk == RiskState()
    assert state.changes == ChangesState()
    assert state.validation == ValidationState()
    assert state.review == ReviewState()
    assert state.failure == FailureState()
    assert state.repair == RepairState(max_cycles=2)

    assert state.run == before.run
    assert state.repository == before.repository
    assert state.project == before.project
    assert state.architecture_invariants == before.architecture_invariants
    assert state.baseline == before.baseline
    assert state.work == before.work
    assert state.work.completed_items == before.work.completed_items
    assert state.work.available_items == before.work.available_items
    assert state.work.delivery_scope == before.work.delivery_scope
    assert state.cancellation == before.cancellation


def test_scope_is_unlocked_and_can_be_redefined_for_second_item() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    state = dirty_more_work_state(engine)
    state, _ = engine.apply_result(state, success("MORE_WORK", state))
    state = replace(state, graph=GraphProgress(current_node="EXPLORE"))

    state, _ = engine.apply_result(
        state,
        success(
            "EXPLORE",
            state,
            PatchOperation.append_unique("scope.included", "src/second.py"),
        ),
    )

    assert state.scope.included == ("src/second.py",)
    assert state.scope.locked is False


def test_more_work_node_cannot_perform_the_item_scope_reset() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    state = dirty_more_work_state(engine)

    with pytest.raises(UnauthorizedStatePatchError):
        engine.apply_result(
            state,
            success(
                "MORE_WORK",
                state,
                PatchOperation.clear("scope.included"),
            ),
        )


def test_second_item_receives_full_independent_repair_budget() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    state = dirty_more_work_state(engine)
    state, _ = engine.apply_result(state, success("MORE_WORK", state))
    assert state.repair.count == 0

    state = replace(state, graph=GraphProgress(current_node="CLASSIFY_FAILURE"))
    for expected_count in (1, 2):
        state, transition = engine.apply_result(
            state,
            success(
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
        state, _ = engine.apply_result(state, success("PROGRAMMER_REPAIR", state))
        state, _ = engine.apply_result(state, failure("VALIDATE"))

    state, transition = engine.apply_result(
        state,
        success(
            "CLASSIFY_FAILURE",
            state,
            PatchOperation.set("repair.classification", RepairClassification.PROGRAMMER),
        ),
    )
    assert transition.to_node == "FINALIZE"
    assert state.repair.count == 2


def test_entering_delivery_review_clears_stale_task_review_decisions() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    state = dirty_more_work_state(engine, has_more_work=False)

    state, transition = engine.apply_result(state, success("MORE_WORK", state))

    assert transition.to_node == "DELIVERY_REVIEW"
    assert state.review == ReviewState()


def test_delivery_review_without_fresh_decision_cannot_reach_checkpoint() -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    state = dirty_more_work_state(engine, has_more_work=False)
    state, _ = engine.apply_result(state, success("MORE_WORK", state))

    with pytest.raises(NoValidTransitionError):
        engine.apply_result(state, success("DELIVERY_REVIEW", state))
