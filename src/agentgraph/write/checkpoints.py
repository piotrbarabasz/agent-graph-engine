"""Write-specific binding and validation for durable human checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from agentgraph.core import CheckpointOutcome, GraphState, RiskLevel
from agentgraph.runtime import CheckpointDecision, CheckpointRequestRecord, CheckpointStore
from agentgraph.runtime.codec import format_timestamp, parse_timestamp, sha256_digest, utc_now
from agentgraph.runtime.errors import CheckpointEvidenceError, CheckpointStoreError
from agentgraph.runtime.state_store import StateStore

from .errors import CheckpointBindingError, CheckpointError
from .models import CheckpointView, WriteInputs
from .workspace import WriteExecution

CHECKPOINT_CODE = "critical_risk_approval_required"
CHECKPOINT_MESSAGE = "Critical-risk work requires an explicit durable human decision."


class WriteCheckpointController:
    """Build and consume exact bindings without exposing approval data to providers."""

    def __init__(
        self,
        execution: WriteExecution,
        *,
        clock: Callable[[], datetime] = utc_now,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.execution = execution
        self.inputs: WriteInputs = execution.inputs
        self.clock = clock
        self.store = CheckpointStore(execution.run_path, clock=clock, nonce_factory=nonce_factory)

    @staticmethod
    def checkpoint_id(state: GraphState) -> str:
        resume = state.graph.pending_resume_node
        if not resume:
            raise CheckpointError("checkpoint_not_pending")
        return f"checkpoint-{state.state_version}-{resume.lower()}"

    def ensure_request(self, state: GraphState) -> CheckpointRequestRecord:
        self._require_pending(state)
        checkpoint_id = self.checkpoint_id(state)
        try:
            existing = self.store.load_request(checkpoint_id)
            binding = self._binding(state)
            if existing is not None:
                self._validate_request(existing, binding)
                return existing
            now = self._now()
            record = CheckpointRequestRecord.create(
                **binding,
                nonce=self.store.new_nonce(),
                created_at=format_timestamp(now),
                expires_at=format_timestamp(
                    now + timedelta(seconds=self.inputs.checkpoint_ttl_seconds)
                ),
            )
            persisted = self.store.write_request_once(record)
            self._validate_request(persisted, binding)
            return persisted
        except CheckpointBindingError:
            raise
        except CheckpointEvidenceError as exc:
            raise CheckpointBindingError(
                "checkpoint_evidence_invalid", "checkpoint request evidence is invalid"
            ) from exc
        except CheckpointStoreError as exc:
            raise CheckpointError(str(exc), str(exc)) from exc

    def view(self, request: CheckpointRequestRecord) -> CheckpointView:
        return CheckpointView(
            request.checkpoint_id,
            request.code,
            request.message,
            request.nonce,
            request.created_at,
            request.expires_at,
            request.pending_resume_node,
        )

    def submit(
        self,
        state: GraphState,
        *,
        checkpoint_id: str,
        nonce: str,
        outcome: CheckpointOutcome,
        actor: str,
    ) -> CheckpointDecision:
        self._require_pending(state)
        expected_id = self.checkpoint_id(state)
        if checkpoint_id != expected_id:
            raise CheckpointError("checkpoint_not_pending")
        request = self.ensure_request(state)
        try:
            if self.store.load_decision(checkpoint_id) is not None:
                raise CheckpointError("checkpoint_already_decided")
        except CheckpointEvidenceError as exc:
            raise CheckpointBindingError(
                "checkpoint_evidence_invalid", "checkpoint decision evidence is invalid"
            ) from exc
        if nonce != request.nonce:
            raise CheckpointError("checkpoint_nonce_mismatch")
        if not isinstance(outcome, CheckpointOutcome):
            raise CheckpointError("checkpoint_outcome_invalid")
        if not isinstance(actor, str) or not actor.strip() or "\x00" in actor or len(actor) > 256:
            raise CheckpointError("checkpoint_actor_invalid")
        decided_at = self._now()
        created_at = parse_timestamp(request.created_at)
        expires_at = parse_timestamp(request.expires_at)
        if decided_at < created_at:
            raise CheckpointError("checkpoint_time_invalid")
        if decided_at > expires_at:
            raise CheckpointError("checkpoint_expired")
        try:
            decision = CheckpointDecision.create(
                schema_version=1,
                checkpoint_id=checkpoint_id,
                request_digest=request.request_digest,
                nonce=request.nonce,
                outcome=outcome,
                actor=actor,
                decided_at=format_timestamp(decided_at),
            )
            self.store.write_decision_once(decision)
            return decision
        except CheckpointStoreError as exc:
            code = str(exc)
            if code == "checkpoint_already_decided":
                raise CheckpointError(code) from exc
            raise CheckpointBindingError("checkpoint_evidence_invalid") from exc

    def decision(
        self, state: GraphState
    ) -> tuple[CheckpointRequestRecord, CheckpointDecision | None]:
        request = self.ensure_request(state)
        try:
            decision = self.store.load_decision(request.checkpoint_id)
        except CheckpointEvidenceError as exc:
            raise CheckpointBindingError(
                "checkpoint_evidence_invalid", "checkpoint decision evidence is invalid"
            ) from exc
        if decision is not None and (
            decision.checkpoint_id != request.checkpoint_id
            or decision.request_digest != request.request_digest
            or decision.nonce != request.nonce
            or parse_timestamp(decision.decided_at) < parse_timestamp(request.created_at)
            or parse_timestamp(decision.decided_at) > parse_timestamp(request.expires_at)
        ):
            raise CheckpointBindingError("checkpoint_binding_mismatch")
        return request, decision

    def expired(self, request: CheckpointRequestRecord) -> bool:
        return self._now() > parse_timestamp(request.expires_at)

    def decision_reference(self, request: CheckpointRequestRecord) -> str:
        return self.store.relative_decision_reference(request.checkpoint_id)

    def _binding(self, state: GraphState) -> dict[str, object]:
        try:
            self.execution.live_recheck("checkpoint_binding_mismatch")
            tree_id = self.execution.git.commit_tree_id(
                self.execution.target, self.inputs.baseline_head
            )
        except Exception as exc:
            raise CheckpointBindingError("checkpoint_binding_mismatch") from exc
        risk = state.risk.level
        if risk is not RiskLevel.CRITICAL:
            raise CheckpointBindingError("checkpoint_binding_mismatch")
        return {
            "schema_version": 1,
            "checkpoint_id": self.checkpoint_id(state),
            "project_id": self.inputs.project_id,
            "run_id": self.execution.run_id,
            "code": CHECKPOINT_CODE,
            "message": CHECKPOINT_MESSAGE,
            "node_id": "HUMAN_CHECKPOINT",
            "pending_resume_node": state.graph.pending_resume_node,
            "state_version": state.state_version,
            "state_digest": StateStore.digest_for_state(state),
            "package_digest": sha256_digest(self.inputs.package),
            "write_inputs_digest": sha256_digest(self.inputs),
            "source_revision": self.inputs.source_revision,
            "baseline_head": self.inputs.baseline_head,
            "baseline_tree_id": tree_id,
            "capability_fingerprint": self.inputs.capability_fingerprint,
            "risk_level": risk,
            "operations_digest": sha256_digest(
                {
                    "changes": state.changes,
                    "validation": state.validation,
                    "review": state.review,
                    "repair": state.repair,
                    "commits": state.commits,
                    "push": state.push,
                    "pull_request": state.pull_request,
                }
            ),
        }

    def _validate_request(
        self, request: CheckpointRequestRecord, binding: dict[str, object]
    ) -> None:
        for name, value in binding.items():
            if getattr(request, name) != value:
                raise CheckpointBindingError("checkpoint_binding_mismatch")
        lifetime = parse_timestamp(request.expires_at) - parse_timestamp(request.created_at)
        if lifetime != timedelta(seconds=self.inputs.checkpoint_ttl_seconds):
            raise CheckpointBindingError("checkpoint_binding_mismatch")

    @staticmethod
    def _require_pending(state: GraphState) -> None:
        if (
            state.run.status.value != "running"
            or state.graph.current_node != "HUMAN_CHECKPOINT"
            or state.graph.pending_resume_node is None
        ):
            raise CheckpointError("checkpoint_not_pending")

    def _now(self) -> datetime:
        value = self.clock()
        format_timestamp(value)
        return value
