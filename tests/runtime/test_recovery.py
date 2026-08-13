from __future__ import annotations

from dataclasses import replace

import pytest

from agentgraph.core import (
    CANONICAL_V1_GRAPH,
    GraphEngine,
    GraphState,
    NodeResult,
    NodeStatus,
    NodeType,
    PatchOperation,
    PolicySnapshot,
    RepairClassification,
    StatePatch,
)
from agentgraph.core.state import GraphProgress, RepairState
from agentgraph.runtime.codec import encode_value
from agentgraph.runtime.journal import Journal, JournalRecordType
from agentgraph.runtime.recovery import RecoveryAction, RecoveryManager
from agentgraph.runtime.state_store import StateStore
from tests.helpers import ScriptedNode


def harness(tmp_path, state: GraphState, engine: GraphEngine):
    store = StateStore(tmp_path / "state.json")
    persisted = store.initialize(state)
    journal = Journal(tmp_path / "journal.jsonl", state.run.run_id)
    journal.initialize()
    journal.append(
        JournalRecordType.RUN_STARTED,
        {"initial_state_version": state.state_version, "initial_state_digest": persisted.digest},
    )
    return store, journal, RecoveryManager(engine, store, journal)


def record_started(journal: Journal, state: GraphState) -> None:
    attempt = f"{state.run.run_id}:{state.graph.current_node}:{state.graph.transition_seq + 1}"
    journal.append(
        JournalRecordType.NODE_STARTED,
        {
            "node_id": state.graph.current_node,
            "attempt_id": attempt,
            "base_state_version": state.state_version,
            "idempotency_key": attempt,
        },
    )


def record_result(engine, journal, state, result):
    next_state, transition = engine.apply_result(state, result)
    journal.append(
        JournalRecordType.NODE_RESULT_RECORDED,
        {
            "node_result": encode_value(result),
            "transition": encode_value(transition),
            "base_state_version": state.state_version,
            "expected_next_state_version": next_state.state_version,
            "expected_next_state_digest": StateStore.digest_for_state(next_state),
        },
    )
    return next_state, transition


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("EXPLORE", RecoveryAction.RERUN_INTERRUPTED_NODE),
        ("HUMAN_CHECKPOINT", RecoveryAction.RERUN_INTERRUPTED_NODE),
        ("IMPLEMENT", RecoveryAction.BLOCKED),
    ],
)
def test_interrupted_node_recovery_depends_on_capability(tmp_path, node_id, expected) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = replace(
        engine.initial_state("run_interrupt"), graph=GraphProgress(current_node=node_id)
    )
    _, journal, recovery = harness(tmp_path, state, engine)
    record_started(journal, state)
    assessment = recovery.assess()
    assert CANONICAL_V1_GRAPH.node(node_id).node_type in {
        NodeType.LLM_READ_ONLY,
        NodeType.LLM_WRITE,
        NodeType.HUMAN_CHECKPOINT,
    }
    assert assessment.action is expected


def test_recorded_result_is_reapplied_without_node_invocation(tmp_path) -> None:
    node = ScriptedNode("START")
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(), {"START": node})
    state = engine.initial_state("run_reapply")
    store, journal, recovery = harness(tmp_path, state, engine)
    context = engine.build_node_context(state)
    result = node.run(state, context)
    record_started(journal, state)
    next_state, _ = record_result(engine, journal, state, result)
    calls_before = node.calls
    assert recovery.assess().action is RecoveryAction.REAPPLY_RECORDED_RESULT
    recovery.execute(recovery.assess())
    assert node.calls == calls_before
    assert store.load() == next_state


def test_recorded_result_attempt_identity_mismatch_blocks_recovery(tmp_path) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = engine.initial_state("run_identity")
    _, journal, recovery = harness(tmp_path, state, engine)
    record_started(journal, state)
    result = NodeResult("START", "different-attempt", NodeStatus.SUCCEEDED)
    record_result(engine, journal, state, result)
    assessment = recovery.assess()
    assert assessment.action is RecoveryAction.BLOCKED
    assert assessment.reason_code == "node_attempt_identity_mismatch"


def test_state_committed_missing_marker_does_not_double_apply(tmp_path) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
    state = engine.initial_state("run_marker")
    store, journal, recovery = harness(tmp_path, state, engine)
    result = NodeResult("START", "run_marker:START:1", NodeStatus.SUCCEEDED)
    record_started(journal, state)
    next_state, _ = record_result(engine, journal, state, result)
    store.compare_and_swap(0, next_state)
    assert recovery.assess().action is RecoveryAction.COMPLETE_TRANSITION_MARKER
    recovery.execute(recovery.assess())
    assert store.load().state_version == 1


@pytest.mark.parametrize("commit_state_first", [False, True])
def test_repair_count_increments_exactly_once_across_recovery(
    tmp_path, commit_state_first: bool
) -> None:
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(max_repair_cycles=2))
    state = replace(
        engine.initial_state("run_repair"),
        graph=GraphProgress(current_node="CLASSIFY_FAILURE"),
        repair=RepairState(max_cycles=2),
    )
    store, journal, recovery = harness(tmp_path, state, engine)
    result = NodeResult(
        "CLASSIFY_FAILURE",
        "run_repair:CLASSIFY_FAILURE:1",
        NodeStatus.SUCCEEDED,
        state_patch=StatePatch(
            0,
            (PatchOperation.set("repair.classification", RepairClassification.PROGRAMMER),),
        ),
    )
    record_started(journal, state)
    next_state, _ = record_result(engine, journal, state, result)
    if commit_state_first:
        store.compare_and_swap(0, next_state)
    recovery.execute(recovery.assess())
    assert store.load().repair.count == 1
