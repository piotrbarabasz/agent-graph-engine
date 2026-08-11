from __future__ import annotations

import pytest

from agentgraph.core import CANONICAL_V1_GRAPH, GraphEngine, PolicySnapshot, RunStatus
from agentgraph.runtime import DurableGraphCoordinator
from agentgraph.runtime.codec import decode_value, parse_json_bytes
from agentgraph.runtime.errors import RunAlreadyExistsError
from agentgraph.runtime.journal import Journal, JournalRecordType
from agentgraph.runtime.receipts import FinalReceipt
from tests.helpers import ScriptedNode


def coordinator(runtime_paths, project, *, fault=None):
    node_ids = ("START", "DISCOVER_PROJECT", "PREFLIGHT", "SELECT_WORK", "FINALIZE")
    nodes = {node_id: ScriptedNode(node_id) for node_id in node_ids}
    engine = GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot(), nodes)
    return DurableGraphCoordinator(runtime_paths, project, engine, fault=fault), nodes


def test_start_and_one_step_follow_durable_record_order(runtime_paths, project) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    handle = runtime.start_run("run_order")
    with runtime.open_session(handle.run_id) as session:
        state = session.step()
    records = Journal(handle.runtime_path / "journal.jsonl", handle.run_id).load()
    assert [item.record_type for item in records] == [
        JournalRecordType.RUN_STARTED,
        JournalRecordType.NODE_STARTED,
        JournalRecordType.NODE_RESULT_RECORDED,
        JournalRecordType.TRANSITION_COMMITTED,
    ]
    assert state.state_version == 1


def test_existing_run_id_is_never_overwritten(runtime_paths, project) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    runtime.start_run("run_existing")
    with pytest.raises(RunAlreadyExistsError):
        runtime.start_run("run_existing")


def test_terminal_run_writes_idempotent_final_receipt(runtime_paths, project) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    handle = runtime.start_run("run_final")
    with runtime.open_session(handle.run_id) as session:
        while session.store.load().graph.current_node != "END":
            session.step()
        receipt = decode_value(
            parse_json_bytes((handle.runtime_path / "final.json").read_bytes()), FinalReceipt
        )
        session._finalize(session.store.load(), session.store.digest())
    assert receipt.final_status == RunStatus.COMPLETED.value
    records = Journal(handle.runtime_path / "journal.jsonl", handle.run_id).load()
    assert sum(item.record_type is JournalRecordType.RUN_FINALIZED for item in records) == 1


def test_run_creation_and_steps_never_write_target_repository(runtime_paths, project) -> None:
    target = project.canonical_root
    before = {
        path.relative_to(target): path.read_bytes()
        for path in __import__("pathlib").Path(target).rglob("*")
        if path.is_file()
    }
    runtime, _ = coordinator(runtime_paths, project)
    handle = runtime.start_run("run_clean_target")
    with runtime.open_session(handle.run_id) as session:
        session.step()
    after = {
        path.relative_to(target): path.read_bytes()
        for path in __import__("pathlib").Path(target).rglob("*")
        if path.is_file()
    }
    assert after == before


def test_fault_after_node_started_is_durable(runtime_paths, project) -> None:
    def fault(stage: str) -> None:
        if stage == "after_node_started":
            raise RuntimeError("crash")

    runtime, nodes = coordinator(runtime_paths, project, fault=fault)
    handle = runtime.start_run("run_started_crash")
    with pytest.raises(RuntimeError, match="crash"), runtime.open_session(handle.run_id) as session:
        session.step()
    assert nodes["START"].calls == 0
    with runtime.open_session(handle.run_id, recovery=True) as session:
        assessment = session.assess_recovery()
    assert assessment.action.value == "rerun_interrupted_node"
