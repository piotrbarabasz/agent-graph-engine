"""Fixed-order durable graph-step coordinator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentgraph.core import GraphEngine, GraphState

from .atomic import atomic_write_bytes
from .codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
    format_timestamp,
    parse_json_bytes,
    utc_now,
)
from .errors import RunAlreadyExistsError, RunNotFoundError
from .ids import generate_run_id
from .journal import Journal, JournalRecordType
from .locking import ProjectLock
from .paths import RuntimePaths
from .project_registry import ProjectRecord
from .receipts import FinalReceipt
from .recovery import RecoveryAssessment, RecoveryManager, _transition_commit_payload
from .state_store import StateStore


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Stable neutral locator for one durable run."""

    project_id: str
    run_id: str
    runtime_path: Path


class DurableGraphCoordinator:
    """Connect GraphEngine to lock, journal, CAS state, recovery, and receipts."""

    def __init__(
        self,
        paths: RuntimePaths,
        project: ProjectRecord,
        engine: GraphEngine,
        *,
        run_id_factory: Callable[[], str] = generate_run_id,
        now: Callable[[], datetime] = utc_now,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = paths
        self.project = project
        self.engine = engine
        self.run_id_factory = run_id_factory
        self.now = now
        self.fault = fault or (lambda stage: None)

    def start_run(self, run_id: str | None = None) -> RunHandle:
        """Atomically initialize state version zero and RUN_STARTED under project lock."""

        selected = run_id or self.run_id_factory()
        run_path = self.paths.run(self.project.project_id, selected)
        with self._lock(selected):
            if run_path.exists():
                raise RunAlreadyExistsError(f"run already exists: {selected}")
            run_path.mkdir(parents=True)
            (run_path / "recovery").mkdir()
            (run_path / "temp").mkdir()
            state = self.engine.initial_state(selected)
            persisted = StateStore(run_path / "state.json").initialize(state)
            journal = Journal(run_path / "journal.jsonl", selected, now=self.now)
            journal.initialize()
            journal.append(
                JournalRecordType.RUN_STARTED,
                {
                    "initial_state_version": state.state_version,
                    "initial_state_digest": persisted.digest,
                },
            )
        return RunHandle(self.project.project_id, selected, run_path)

    def open_session(self, run_id: str, *, recovery: bool = False) -> RuntimeSession:
        """Return a context manager holding the project lock for all operations."""

        run_path = self.paths.run(self.project.project_id, run_id)
        if not run_path.is_dir():
            raise RunNotFoundError(f"run not found: {run_id}")
        return RuntimeSession(self, RunHandle(self.project.project_id, run_id, run_path), recovery)

    def _lock(self, run_id: str, *, recovery: bool = False) -> ProjectLock:
        return ProjectLock(
            self.paths.project_lock(self.project.project_id),
            self.paths.lease(self.project.project_id),
            project_id=self.project.project_id,
            run_id=run_id,
            recovery=recovery,
            now=self.now,
            recovery_evidence_dir=self.paths.run(self.project.project_id, run_id) / "recovery",
        )


class RuntimeSession:
    """One locked writer session for durable steps and recovery."""

    def __init__(
        self, coordinator: DurableGraphCoordinator, handle: RunHandle, recovery: bool
    ) -> None:
        self.coordinator = coordinator
        self.handle = handle
        self.lock = coordinator._lock(handle.run_id, recovery=recovery)
        self.store = StateStore(handle.runtime_path / "state.json")
        self.journal = Journal(
            handle.runtime_path / "journal.jsonl", handle.run_id, now=coordinator.now
        )
        self._entered = False

    def __enter__(self) -> RuntimeSession:
        self.lock.acquire()
        self._entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._entered = False
        self.lock.release()

    def step(self) -> GraphState:
        """Execute one node using the fixed durable commit protocol."""

        self._require_lock()
        engine = self.coordinator.engine
        state = self.store.load()
        context = engine.build_node_context(state)
        self.coordinator.fault("before_node_started")
        self.journal.append(
            JournalRecordType.NODE_STARTED,
            {
                "node_id": state.graph.current_node,
                "attempt_id": context.node_attempt_id,
                "base_state_version": state.state_version,
                "idempotency_key": context.idempotency_key,
            },
        )
        self.coordinator.fault("after_node_started")
        result = engine.invoke_node(state, context)
        self.coordinator.fault("after_node_invocation")
        next_state, transition = engine.apply_result(state, result)
        expected_digest = StateStore.digest_for_state(next_state)
        self.journal.append(
            JournalRecordType.NODE_RESULT_RECORDED,
            {
                "node_result": encode_value(result),
                "transition": encode_value(transition),
                "base_state_version": state.state_version,
                "expected_next_state_version": next_state.state_version,
                "expected_next_state_digest": expected_digest,
            },
        )
        self.coordinator.fault("after_node_result_recorded")
        self.coordinator.fault("before_state_cas")
        persisted = self.store.compare_and_swap(state.state_version, next_state)
        self.coordinator.fault("after_state_cas")
        self.coordinator.fault("before_transition_committed")
        self.journal.append(
            JournalRecordType.TRANSITION_COMMITTED,
            _transition_commit_payload(transition, next_state.state_version, persisted.digest),
        )
        self.coordinator.fault("after_transition_committed")
        self.lock.heartbeat()
        if next_state.graph.current_node == "END":
            self._finalize(next_state, persisted.digest)
        return next_state

    def assess_recovery(self) -> RecoveryAssessment:
        """Assess current durable evidence without invoking a node."""

        self._require_lock()
        return RecoveryManager(self.coordinator.engine, self.store, self.journal).assess()

    def recover(self) -> RecoveryAssessment:
        """Complete a provable interrupted commit phase without graph-semantic changes."""

        self._require_lock()
        manager = RecoveryManager(self.coordinator.engine, self.store, self.journal)
        assessment = manager.assess()
        result = manager.execute(assessment)
        state = self.store.load_persisted()
        if state.state.graph.current_node == "END":
            self._finalize(state.state, state.digest)
        return result

    def repair_truncated_journal(self) -> None:
        """Preserve and repair only an incomplete final journal tail."""

        self._require_lock()
        self.journal.repair_truncated_tail(self.handle.runtime_path / "recovery")

    def _finalize(self, state: GraphState, digest: str) -> None:
        records = self.journal.load()
        if not any(item.record_type is JournalRecordType.RUN_FINALIZED for item in records):
            self.journal.append(
                JournalRecordType.RUN_FINALIZED,
                {
                    "final_state_version": state.state_version,
                    "final_state_digest": digest,
                    "final_status": state.run.status.value,
                },
            )
            records = self.journal.load()
        receipt = FinalReceipt(
            self.handle.project_id,
            self.handle.run_id,
            state.run.status.value,
            state.state_version,
            digest,
            records[-1].seq,
            format_timestamp(self.coordinator.now()),
        )
        path = self.handle.runtime_path / "final.json"
        if path.exists():
            existing = decode_value(parse_json_bytes(path.read_bytes()), FinalReceipt)
            comparable = (
                existing.project_id,
                existing.run_id,
                existing.final_status,
                existing.final_state_version,
                existing.final_state_digest,
                existing.last_journal_seq,
            )
            expected = (
                receipt.project_id,
                receipt.run_id,
                receipt.final_status,
                receipt.final_state_version,
                receipt.final_state_digest,
                receipt.last_journal_seq,
            )
            if comparable != expected:
                raise RunAlreadyExistsError("terminal receipt conflicts with persisted state")
            return
        self.coordinator.fault("before_final_receipt")
        atomic_write_bytes(path, canonical_json_bytes(receipt))
        self.coordinator.fault("after_final_receipt")

    def _require_lock(self) -> None:
        if not self._entered:
            raise RuntimeError("runtime session must be entered before use")
