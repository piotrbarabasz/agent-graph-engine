"""Fixed-order durable graph-step coordinator."""

from __future__ import annotations

import os
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
from .errors import (
    ActiveRunExistsError,
    AgentGraphRuntimeError,
    IncompleteRunInitializationError,
    InvalidRuntimeIdentifierError,
    RunAlreadyExistsError,
    RunNotFoundError,
    SerializationError,
)
from .ids import generate_run_id, validate_run_id
from .journal import Journal, JournalRecordType
from .lifecycle import ActiveRunRecord
from .locking import ProjectLock
from .paths import RuntimePaths
from .project_registry import ProjectRecord
from .receipts import FinalReceipt
from .recovery import (
    InterruptedSideEffectReconciler,
    RecoveryAssessment,
    RecoveryManager,
    _transition_commit_payload,
)
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
        side_effect_reconciler: InterruptedSideEffectReconciler | None = None,
    ) -> None:
        self.paths = paths
        self.project = project
        self.engine = engine
        self.run_id_factory = run_id_factory
        self.now = now
        self.fault = fault or (lambda stage: None)
        self.side_effect_reconciler = side_effect_reconciler

    def start_run(
        self,
        run_id: str | None = None,
        *,
        initialize_artifacts: Callable[[Path], None] | None = None,
    ) -> RunHandle:
        """Atomically initialize state version zero and RUN_STARTED under project lock."""

        selected = self.run_id_factory() if run_id is None else run_id
        validate_run_id(selected)
        return self._start_run(
            selected,
            recover_incomplete=False,
            initialize_artifacts=initialize_artifacts,
        )

    def recover_incomplete_run_initialization(
        self,
        run_id: str,
        *,
        initialize_artifacts: Callable[[Path], None] | None = None,
    ) -> RunHandle:
        """Preserve staging evidence and explicitly retry one incomplete initialization."""

        validate_run_id(run_id)
        return self._start_run(
            run_id,
            recover_incomplete=True,
            initialize_artifacts=initialize_artifacts,
        )

    def _start_run(
        self,
        selected: str,
        *,
        recover_incomplete: bool,
        initialize_artifacts: Callable[[Path], None] | None,
    ) -> RunHandle:
        validate_run_id(selected)
        run_path = self.paths.run(self.project.project_id, selected)
        staging = self.paths.initializing_run(self.project.project_id, selected)
        with self._lock(selected):
            if run_path.exists():
                raise RunAlreadyExistsError(f"run already exists: {selected}")
            self._enforce_active_run_lifecycle()
            if staging.exists():
                if not recover_incomplete:
                    raise IncompleteRunInitializationError(
                        f"incomplete staging run requires explicit recovery: {selected}"
                    )
                self._preserve_incomplete_staging(staging, selected)
            staging.mkdir(parents=True)
            self.fault("after_run_staging_creation")
            (staging / "recovery").mkdir()
            (staging / "temp").mkdir()
            state = self.engine.initial_state(selected)
            persisted = StateStore(staging / "state.json").initialize(state)
            self.fault("after_state_initialization")
            journal = Journal(staging / "journal.jsonl", selected, now=self.now)
            journal.initialize()
            self.fault("after_journal_creation")
            journal.append(
                JournalRecordType.RUN_STARTED,
                {
                    "initial_state_version": state.state_version,
                    "initial_state_digest": persisted.digest,
                },
            )
            self.fault("after_run_started")
            if initialize_artifacts is not None:
                initialize_artifacts(staging)
                self._fsync_directory(staging)
            self.fault("after_initialization_artifacts")
            self.fault("before_run_promotion")
            os.replace(staging, run_path)
            self._fsync_runs_directory()
            self.fault("after_run_promotion")
            self.fault("before_run_activation")
            self._write_active_run(selected)
            self.fault("after_run_activation")
        return RunHandle(self.project.project_id, selected, run_path)

    def open_session(self, run_id: str, *, recovery: bool = False) -> RuntimeSession:
        """Return a context manager holding the project lock for all operations."""

        validate_run_id(run_id)
        run_path = self.paths.run(self.project.project_id, run_id)
        if not run_path.is_dir():
            if self.paths.initializing_run(self.project.project_id, run_id).exists():
                raise IncompleteRunInitializationError(
                    f"run initialization is incomplete: {run_id}"
                )
            raise RunNotFoundError(f"run not found: {run_id}")
        self._validate_initialized_run(run_id)
        return RuntimeSession(self, RunHandle(self.project.project_id, run_id, run_path), recovery)

    def _enforce_active_run_lifecycle(self) -> None:
        active = self._read_active_run()
        if active is not None:
            if self._has_terminal_evidence(active.run_id):
                self._clear_active_run(active.run_id)
            else:
                raise ActiveRunExistsError(f"unfinished active run exists: {active.run_id}")
        unfinished = self._find_unfinished_runs()
        if unfinished:
            self._write_active_run(unfinished[0])
            raise ActiveRunExistsError(f"unfinished active run exists: {unfinished[0]}")

    def _find_unfinished_runs(self) -> tuple[str, ...]:
        runs_dir = self.paths.project(self.project.project_id) / "runs"
        unfinished = []
        for path in runs_dir.iterdir():
            if not path.is_dir() or path.name.startswith("."):
                continue
            self._validate_initialized_run(path.name)
            if not self._has_terminal_evidence(path.name):
                unfinished.append(path.name)
        return tuple(sorted(unfinished))

    def _validate_initialized_run(self, run_id: str) -> None:
        run_path = self.paths.run(self.project.project_id, run_id)
        try:
            persisted = StateStore(run_path / "state.json").load_persisted()
            records = Journal(run_path / "journal.jsonl", run_id, now=self.now).load()
            first = records[0]
            if (
                first.record_type is not JournalRecordType.RUN_STARTED
                or first.payload.get("initial_state_version") != 0
                or persisted.state.run.run_id != run_id
                or (
                    persisted.state.state_version == 0
                    and first.payload.get("initial_state_digest") != persisted.digest
                )
            ):
                raise IncompleteRunInitializationError("RUN_STARTED evidence mismatch")
        except (IndexError, OSError, AgentGraphRuntimeError) as exc:
            if isinstance(exc, IncompleteRunInitializationError):
                raise
            raise IncompleteRunInitializationError(
                f"canonical run initialization is incomplete: {run_id}"
            ) from exc

    def _has_terminal_evidence(self, run_id: str) -> bool:
        run_path = self.paths.run(self.project.project_id, run_id)
        try:
            persisted = StateStore(run_path / "state.json").load_persisted()
            if persisted.state.graph.current_node != "END":
                return False
            records = Journal(run_path / "journal.jsonl", run_id, now=self.now).load()
            finalized = next(
                (
                    record
                    for record in reversed(records)
                    if record.record_type is not JournalRecordType.RECOVERY_NOTE
                ),
                None,
            )
            receipt = decode_value(
                parse_json_bytes((run_path / "final.json").read_bytes()), FinalReceipt
            )
            return (
                finalized is not None
                and finalized.record_type is JournalRecordType.RUN_FINALIZED
                and receipt.project_id == self.project.project_id
                and receipt.run_id == run_id
                and receipt.final_status == persisted.state.run.status.value
                and receipt.final_state_version == persisted.state.state_version
                and receipt.final_state_digest == persisted.digest
            )
        except (IndexError, OSError, AgentGraphRuntimeError):
            return False

    def _read_active_run(self) -> ActiveRunRecord | None:
        path = self.paths.active_run(self.project.project_id)
        if not path.exists():
            return None
        try:
            record = decode_value(parse_json_bytes(path.read_bytes()), ActiveRunRecord)
        except (OSError, SerializationError, InvalidRuntimeIdentifierError) as exc:
            raise ActiveRunExistsError("active-run ownership is corrupt") from exc
        if record.project_id != self.project.project_id:
            raise ActiveRunExistsError("active-run project identity mismatch")
        return record

    def _write_active_run(self, run_id: str) -> None:
        record = ActiveRunRecord(
            self.project.project_id,
            run_id,
            format_timestamp(self.now()),
        )
        atomic_write_bytes(
            self.paths.active_run(self.project.project_id), canonical_json_bytes(record)
        )

    def _clear_active_run(self, run_id: str) -> None:
        active = self._read_active_run()
        if active is not None and active.run_id == run_id:
            self.paths.active_run(self.project.project_id).unlink()
            self._fsync_project_directory()

    def _assert_session_ownership(self, run_id: str) -> None:
        state = StateStore(self.paths.run(self.project.project_id, run_id) / "state.json").load()
        if state.graph.current_node == "END":
            return
        active = self._read_active_run()
        if active is None:
            self._write_active_run(run_id)
            return
        if active.run_id != run_id:
            raise ActiveRunExistsError(f"another unfinished run owns the project: {active.run_id}")

    def _preserve_incomplete_staging(self, staging: Path, run_id: str) -> None:
        recovery_root = self.paths.initialization_recovery(self.project.project_id)
        recovery_root.mkdir(parents=True, exist_ok=True)
        index = 1
        while (destination := recovery_root / f"{run_id}-{index}").exists():
            index += 1
        os.replace(staging, destination)

    def _fsync_runs_directory(self) -> None:
        if os.name == "nt":
            return
        runs = self.paths.project(self.project.project_id) / "runs"
        descriptor = os.open(runs, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_project_directory(self) -> None:
        if os.name == "nt":
            return
        project = self.paths.project(self.project.project_id)
        descriptor = os.open(project, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

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
        try:
            self.coordinator._assert_session_ownership(self.handle.run_id)
            self._entered = True
            return self
        except BaseException:
            self.lock.release()
            raise

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
        return RecoveryManager(
            self.coordinator.engine,
            self.store,
            self.journal,
            self.coordinator.side_effect_reconciler,
        ).assess()

    def recover(self) -> RecoveryAssessment:
        """Complete a provable interrupted commit phase without graph-semantic changes."""

        self._require_lock()
        manager = RecoveryManager(
            self.coordinator.engine,
            self.store,
            self.journal,
            self.coordinator.side_effect_reconciler,
        )
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
            self.coordinator._clear_active_run(self.handle.run_id)
            return
        self.coordinator.fault("before_final_receipt")
        atomic_write_bytes(path, canonical_json_bytes(receipt))
        self.coordinator.fault("after_final_receipt")
        self.coordinator._clear_active_run(self.handle.run_id)

    def _require_lock(self) -> None:
        if not self._entered:
            raise RuntimeError("runtime session must be entered before use")
