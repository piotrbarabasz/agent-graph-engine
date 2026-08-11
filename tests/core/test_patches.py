from dataclasses import replace

import pytest

from agentgraph.core import (
    InvalidStatePatchError,
    PatchOperation,
    StaleStatePatchError,
    StatePatch,
    StatePatchApplier,
    UnauthorizedStatePatchError,
    WorkItem,
)
from agentgraph.core.state import GraphState, ScopeState, WorkState


def apply(state: GraphState, *operations: PatchOperation) -> GraphState:
    return StatePatchApplier().apply(
        state,
        StatePatch(state.state_version, operations),
        node_id="TEST",
        allowed_paths=("*",),
    )


def test_set_append_unique_clear_and_merge_by_id() -> None:
    state = GraphState.initial("run")
    first = WorkItem("A")
    second = WorkItem("B")
    state = apply(
        state,
        PatchOperation.set("work.item", first),
        PatchOperation.append_unique("changes.agent_reported_files", "src/a.py"),
        PatchOperation.append_unique("changes.agent_reported_files", "src/a.py"),
        PatchOperation.merge_by_id("work.completed_items", first),
        PatchOperation.merge_by_id("work.completed_items", second),
        PatchOperation.increment("changes.count", 2),
    )

    assert state.state_version == 1
    assert state.work.item == first
    assert state.changes.agent_reported_files == ("src/a.py",)
    assert state.work.completed_items == (first, second)
    assert state.changes.count == 2
    assert apply(state, PatchOperation.clear("work.item")).work.item is None


def test_merge_by_id_replaces_existing_value() -> None:
    state = replace(
        GraphState.initial("run"),
        work=WorkState(completed_items=(WorkItem("A", "old"),)),
    )
    updated = apply(
        state,
        PatchOperation.merge_by_id("work.completed_items", WorkItem("A", "new")),
    )
    assert updated.work.completed_items == (WorkItem("A", "new"),)


def test_stale_patch_is_rejected() -> None:
    state = GraphState.initial("run")
    with pytest.raises(StaleStatePatchError):
        StatePatchApplier().apply(
            state,
            StatePatch(99, (PatchOperation.set("project.name", "x"),)),
            node_id="TEST",
            allowed_paths=("project.*",),
        )


@pytest.mark.parametrize(
    "path,value",
    [
        ("state_version", 10),
        ("graph.current_node", "END"),
        ("graph.previous_node", "END"),
        ("repair.count", 1),
        ("repair.max_cycles", 100),
        ("run.status", "completed"),
        ("commits.records", ("x",)),
        ("push.records", ("x",)),
        ("pull_request.records", ("x",)),
        ("checkpoints.records", ("x",)),
    ],
)
def test_engine_owned_paths_are_rejected(path: str, value: object) -> None:
    state = GraphState.initial("run")
    with pytest.raises(UnauthorizedStatePatchError):
        apply(state, PatchOperation.set(path, value))


def test_node_specific_ownership_is_enforced() -> None:
    state = GraphState.initial("run")
    with pytest.raises(UnauthorizedStatePatchError):
        StatePatchApplier().apply(
            state,
            StatePatch(0, (PatchOperation.set("risk.level", None),)),
            node_id="DISCOVER_PROJECT",
            allowed_paths=("project.*",),
        )


def test_scope_is_frozen_after_implement_entry() -> None:
    state = replace(GraphState.initial("run"), scope=ScopeState(locked=True))
    with pytest.raises(UnauthorizedStatePatchError):
        apply(state, PatchOperation.append_unique("scope.included", "src"))


@pytest.mark.parametrize(
    "operation",
    [
        PatchOperation.set("risk.level", "high"),
        PatchOperation.append_unique("review.safe_to_close", True),
        PatchOperation.increment("changes.agent_reported_files"),
        PatchOperation.set("unknown.path", "x"),
    ],
)
def test_illegal_value_operation_or_path_is_rejected(operation: PatchOperation) -> None:
    with pytest.raises(InvalidStatePatchError):
        apply(GraphState.initial("run"), operation)


def test_merge_by_id_is_not_a_legal_scalar_tuple_operation() -> None:
    with pytest.raises(InvalidStatePatchError, match=r"not legal for requirements\.items"):
        apply(
            GraphState.initial("run"),
            PatchOperation.merge_by_id("requirements.items", "REQ-1"),
        )
