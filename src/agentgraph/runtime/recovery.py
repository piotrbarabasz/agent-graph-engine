"""Deterministic assessment and completion of interrupted durable steps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from agentgraph.core import AgentGraphError, GraphEngine, NodeResult, NodeType, Transition

from .codec import decode_value, encode_value
from .errors import AgentGraphRuntimeError, RecoveryError
from .journal import Journal, JournalRecord, JournalRecordType
from .state_store import StateStore


class RecoveryAction(StrEnum):
    """Typed safe action selected from persisted evidence."""

    CLEAN_RESUME = "clean_resume"
    RERUN_INTERRUPTED_NODE = "rerun_interrupted_node"
    REAPPLY_RECORDED_RESULT = "reapply_recorded_result"
    COMPLETE_TRANSITION_MARKER = "complete_transition_marker"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    """Typed recovery decision; reason text never controls behavior."""

    action: RecoveryAction
    reason_code: str
    human_readable_reason: str
    run_id: str
    persisted_state_version: int
    last_journal_seq: int
    resume_node: str | None
    evidence: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


class RecoveryManager:
    """Assess evidence and complete only provably idempotent protocol phases."""

    def __init__(self, engine: GraphEngine, store: StateStore, journal: Journal) -> None:
        self.engine = engine
        self.store = store
        self.journal = journal

    def assess(self) -> RecoveryAssessment:
        persisted = self.store.load_persisted()
        records = self.journal.load()
        protocol_records = tuple(
            record
            for record in records
            if record.record_type is not JournalRecordType.RECOVERY_NOTE
        )
        if not protocol_records:
            return self._assessment(
                RecoveryAction.BLOCKED,
                "missing_journal_evidence",
                "No durable protocol record exists.",
                persisted.state,
                records,
            )
        last = protocol_records[-1]
        if last.record_type is JournalRecordType.RUN_FINALIZED:
            if (
                persisted.state.graph.current_node == "END"
                and last.payload.get("final_state_version") == persisted.state.state_version
                and last.payload.get("final_state_digest") == persisted.digest
                and last.payload.get("final_status") == persisted.state.run.status.value
            ):
                return self._assessment(
                    RecoveryAction.COMPLETED,
                    "run_finalized",
                    "The terminal run is fully journaled.",
                    persisted.state,
                    records,
                )
            return self._blocked("finalized_non_terminal_state", persisted.state, records)
        if last.record_type is JournalRecordType.RUN_STARTED:
            if (
                last.payload.get("initial_state_version") != persisted.state.state_version
                or last.payload.get("initial_state_digest") != persisted.digest
            ):
                return self._blocked("initial_state_journal_mismatch", persisted.state, records)
            return self._clean_or_completed(persisted.state, records)
        if last.record_type is JournalRecordType.NODE_STARTED:
            return self._assess_started(last, persisted.state, records)
        if last.record_type is JournalRecordType.NODE_RESULT_RECORDED:
            return self._assess_recorded(last, persisted, records)
        if last.record_type is JournalRecordType.TRANSITION_COMMITTED:
            payload = last.payload
            if (
                payload.get("committed_state_version") != persisted.state.state_version
                or payload.get("committed_state_digest") != persisted.digest
                or payload.get("to_node") != persisted.state.graph.current_node
            ):
                return self._blocked(
                    "committed_transition_state_mismatch", persisted.state, records
                )
            recorded = next(
                (
                    item
                    for item in reversed(protocol_records[:-1])
                    if item.record_type is JournalRecordType.NODE_RESULT_RECORDED
                ),
                None,
            )
            if recorded is None or not self._marker_matches_recorded_result(payload, recorded):
                return self._blocked(
                    "transition_marker_evidence_mismatch", persisted.state, records
                )
            return self._clean_or_completed(persisted.state, records)
        return self._blocked("unsupported_journal_tail", persisted.state, records)

    def execute(self, assessment: RecoveryAssessment) -> RecoveryAssessment:
        """Complete a recorded-result CAS or missing marker without invoking a node."""

        if assessment.action not in {
            RecoveryAction.REAPPLY_RECORDED_RESULT,
            RecoveryAction.COMPLETE_TRANSITION_MARKER,
        }:
            return assessment
        records = self.journal.load()
        recorded = next(
            (
                item
                for item in reversed(records)
                if item.record_type is JournalRecordType.NODE_RESULT_RECORDED
            ),
            None,
        )
        if recorded is None:
            raise RecoveryError("recorded result disappeared during recovery")
        payload = recorded.payload
        if assessment.action is RecoveryAction.REAPPLY_RECORDED_RESULT:
            state = self.store.load()
            result = decode_value(payload["node_result"], NodeResult)
            next_state, transition = self.engine.apply_result(state, result)
            self._verify_recomputed(payload, next_state, transition)
            persisted = self.store.compare_and_swap(state.state_version, next_state)
        else:
            persisted = self.store.load_persisted()
            transition = decode_value(payload["transition"], Transition)
        self.journal.append(
            JournalRecordType.TRANSITION_COMMITTED,
            _transition_commit_payload(transition, persisted.state.state_version, persisted.digest),
        )
        return self.assess()

    def _assess_started(
        self, record: JournalRecord, state: Any, records: tuple
    ) -> RecoveryAssessment:
        if record.payload.get("base_state_version") != state.state_version:
            return self._blocked("started_attempt_state_mismatch", state, records)
        node_id = record.payload.get("node_id")
        if node_id != state.graph.current_node:
            return self._blocked("started_attempt_node_mismatch", state, records)
        node_type = self.engine.graph.node(node_id).node_type
        if node_type in {
            NodeType.DETERMINISTIC,
            NodeType.LLM_READ_ONLY,
            NodeType.HUMAN_CHECKPOINT,
        }:
            return self._assessment(
                RecoveryAction.RERUN_INTERRUPTED_NODE,
                "safe_capability_rerun",
                "Interrupted node capability is safe to invoke again.",
                state,
                records,
            )
        return self._assessment(
            RecoveryAction.BLOCKED,
            "unreconciled_side_effect_capability",
            "Interrupted node may have unobserved side effects.",
            state,
            records,
        )

    def _assess_recorded(
        self, record: JournalRecord, persisted: Any, records: tuple
    ) -> RecoveryAssessment:
        payload = record.payload
        started = next(
            (
                item
                for item in reversed(records)
                if item.seq < record.seq and item.record_type is JournalRecordType.NODE_STARTED
            ),
            None,
        )
        try:
            result = decode_value(payload["node_result"], NodeResult)
        except (KeyError, AgentGraphError, AgentGraphRuntimeError):
            return self._blocked("recorded_result_contract_invalid", persisted.state, records)
        if (
            started is None
            or started.payload.get("attempt_id") != result.attempt_id
            or started.payload.get("node_id") != result.node_id
            or started.payload.get("base_state_version") != payload.get("base_state_version")
            or started.payload.get("idempotency_key") != result.attempt_id
        ):
            return self._blocked("node_attempt_identity_mismatch", persisted.state, records)
        base_version = payload.get("base_state_version")
        next_version = payload.get("expected_next_state_version")
        expected_digest = payload.get("expected_next_state_digest")
        if persisted.state.state_version == base_version:
            try:
                next_state, transition = self.engine.apply_result(persisted.state, result)
                self._verify_recomputed(payload, next_state, transition)
            except (KeyError, AgentGraphError, AgentGraphRuntimeError) as exc:
                return self._blocked(
                    "recorded_result_recompute_mismatch",
                    persisted.state,
                    records,
                    {"error": str(exc)},
                )
            return self._assessment(
                RecoveryAction.REAPPLY_RECORDED_RESULT,
                "recorded_result_state_not_committed",
                "Recorded result can be deterministically reapplied without invoking the node.",
                persisted.state,
                records,
            )
        if persisted.state.state_version == next_version and persisted.digest == expected_digest:
            return self._assessment(
                RecoveryAction.COMPLETE_TRANSITION_MARKER,
                "state_committed_marker_missing",
                "State is committed; only the transition marker is missing.",
                persisted.state,
                records,
            )
        return self._blocked("recorded_result_state_mismatch", persisted.state, records)

    @staticmethod
    def _marker_matches_recorded_result(marker: Mapping[str, Any], recorded: JournalRecord) -> bool:
        transition = recorded.payload.get("transition")
        if not isinstance(transition, Mapping):
            return False
        return (
            marker.get("from_node") == transition.get("from_node")
            and marker.get("to_node") == transition.get("to_node")
            and marker.get("edge_id") == transition.get("edge_id")
            and marker.get("committed_state_version")
            == recorded.payload.get("expected_next_state_version")
            and marker.get("committed_state_digest")
            == recorded.payload.get("expected_next_state_digest")
        )

    def _verify_recomputed(
        self, payload: Mapping[str, Any], state: Any, transition: Transition
    ) -> None:
        if payload.get("expected_next_state_version") != state.state_version:
            raise RecoveryError("recomputed state version differs")
        if payload.get("expected_next_state_digest") != StateStore.digest_for_state(state):
            raise RecoveryError("recomputed state digest differs")
        if payload.get("transition") != encode_value(transition):
            raise RecoveryError("recomputed transition differs")

    def _clean_or_completed(self, state: Any, records: tuple) -> RecoveryAssessment:
        action = (
            RecoveryAction.COMPLETED
            if state.graph.current_node == "END"
            else RecoveryAction.CLEAN_RESUME
        )
        code = "terminal_state" if action is RecoveryAction.COMPLETED else "committed_state"
        return self._assessment(
            action, code, "Persisted state matches durable history.", state, records
        )

    def _blocked(
        self, code: str, state: Any, records: tuple, evidence: Mapping[str, Any] | None = None
    ) -> RecoveryAssessment:
        return self._assessment(
            RecoveryAction.BLOCKED,
            code,
            "Persisted evidence is insufficient for automatic recovery.",
            state,
            records,
            evidence,
        )

    def _assessment(
        self,
        action: RecoveryAction,
        code: str,
        reason: str,
        state: Any,
        records: tuple,
        evidence: Mapping[str, Any] | None = None,
    ) -> RecoveryAssessment:
        return RecoveryAssessment(
            action,
            code,
            reason,
            self.journal.run_id,
            state.state_version,
            records[-1].seq if records else 0,
            state.graph.current_node if state.graph.current_node != "END" else None,
            evidence or {},
        )


def _transition_commit_payload(
    transition: Transition, state_version: int, state_digest: str
) -> dict[str, Any]:
    return {
        "from_node": transition.from_node,
        "to_node": transition.to_node,
        "edge_id": transition.edge_id,
        "committed_state_version": state_version,
        "committed_state_digest": state_digest,
    }
