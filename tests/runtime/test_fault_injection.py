from __future__ import annotations

from collections.abc import Callable

import pytest

from agentgraph.core import CANONICAL_V1_GRAPH, GraphEngine, PolicySnapshot
from agentgraph.runtime import DurableGraphCoordinator
from agentgraph.runtime.recovery import RecoveryAction
from tests.helpers import ScriptedNode


def make_runtime(runtime_paths, project, fault: Callable[[str], None] | None = None):
    nodes = {"START": ScriptedNode("START")}
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(), nodes)
    return DurableGraphCoordinator(runtime_paths, project, engine, fault=fault), nodes


@pytest.mark.parametrize(
    ("crash_stage", "expected_action", "expected_calls", "expected_version"),
    [
        ("before_node_started", RecoveryAction.CLEAN_RESUME, 0, 0),
        ("after_node_started", RecoveryAction.RERUN_INTERRUPTED_NODE, 0, 0),
        ("after_node_invocation", RecoveryAction.RERUN_INTERRUPTED_NODE, 1, 0),
        ("after_node_result_recorded", RecoveryAction.REAPPLY_RECORDED_RESULT, 1, 0),
        ("before_state_cas", RecoveryAction.REAPPLY_RECORDED_RESULT, 1, 0),
        ("after_state_cas", RecoveryAction.COMPLETE_TRANSITION_MARKER, 1, 1),
        ("before_transition_committed", RecoveryAction.COMPLETE_TRANSITION_MARKER, 1, 1),
        ("after_transition_committed", RecoveryAction.CLEAN_RESUME, 1, 1),
    ],
)
def test_durable_step_fault_matrix(
    runtime_paths,
    project,
    crash_stage: str,
    expected_action: RecoveryAction,
    expected_calls: int,
    expected_version: int,
) -> None:
    def fault(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError("injected crash")

    runtime, nodes = make_runtime(runtime_paths, project, fault)
    handle = runtime.start_run(f"run_{crash_stage}")
    with (
        pytest.raises(RuntimeError, match="injected"),
        runtime.open_session(handle.run_id) as session,
    ):
        session.step()

    runtime.fault = lambda stage: None
    with runtime.open_session(handle.run_id, recovery=True) as session:
        assessment = session.assess_recovery()
        assert assessment.action is expected_action
        assert session.store.load().state_version == expected_version
        if expected_action in {
            RecoveryAction.REAPPLY_RECORDED_RESULT,
            RecoveryAction.COMPLETE_TRANSITION_MARKER,
        }:
            session.recover()
            assert session.store.load().state_version == 1
    assert nodes["START"].calls == expected_calls


def test_failure_during_state_atomic_write_leaves_recoverable_recorded_result(
    runtime_paths, project, monkeypatch
) -> None:
    from pathlib import Path

    target = Path(project.canonical_root)
    marker = target / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob("*")}
    runtime, nodes = make_runtime(runtime_paths, project)
    handle = runtime.start_run("run_atomic_fault")
    import agentgraph.runtime.state_store as state_store_module

    original = state_store_module.atomic_write_bytes

    def fail_state_write(path, data):
        del data
        if path.name == "state.json":
            raise OSError("atomic replace interrupted")

    monkeypatch.setattr(state_store_module, "atomic_write_bytes", fail_state_write)
    with (
        pytest.raises(OSError, match="interrupted"),
        runtime.open_session(handle.run_id) as session,
    ):
        session.step()
    monkeypatch.setattr(state_store_module, "atomic_write_bytes", original)

    with runtime.open_session(handle.run_id, recovery=True) as session:
        assert session.assess_recovery().action is RecoveryAction.REAPPLY_RECORDED_RESULT
        session.recover()
        assert session.store.load().state_version == 1
    assert nodes["START"].calls == 1
    after = {path.relative_to(target): path.read_bytes() for path in target.rglob("*")}
    assert after == before


def test_terminal_receipt_fault_is_idempotently_completed(runtime_paths, project) -> None:
    node_ids = ("START", "DISCOVER_PROJECT", "PREFLIGHT", "SELECT_WORK", "FINALIZE")
    nodes = {node_id: ScriptedNode(node_id) for node_id in node_ids}
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(), nodes)

    def fault(stage: str) -> None:
        if stage == "before_final_receipt":
            raise RuntimeError("receipt crash")

    runtime = DurableGraphCoordinator(runtime_paths, project, engine, fault=fault)
    handle = runtime.start_run("run_receipt_fault")
    with (
        pytest.raises(RuntimeError, match="receipt"),
        runtime.open_session(handle.run_id) as session,
    ):
        while session.store.load().graph.current_node != "END":
            session.step()
    assert not (handle.runtime_path / "final.json").exists()

    runtime.fault = lambda stage: None
    with runtime.open_session(handle.run_id, recovery=True) as session:
        assert session.recover().action is RecoveryAction.COMPLETED
    assert (handle.runtime_path / "final.json").exists()
