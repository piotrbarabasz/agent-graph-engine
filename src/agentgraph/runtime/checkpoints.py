"""Runtime-neutral immutable storage for durable human checkpoint evidence."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from agentgraph.core import CheckpointOutcome, RiskLevel

from .atomic import atomic_write_bytes
from .codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
    parse_json_bytes,
    parse_timestamp,
    sha256_digest,
    utc_now,
)
from .errors import CheckpointEvidenceError, CheckpointStoreError, SerializationError
from .locking import AdvisoryFileLock

_CHECKPOINT_ID = re.compile(r"checkpoint-[A-Za-z0-9_-]+", re.ASCII)
MAX_ACTOR_LENGTH = 256


def _digest_without(value: object, field_name: str) -> str:
    payload = {
        field.name: getattr(value, field.name)
        for field in fields(value)
        if field.name != field_name
    }
    return sha256_digest(payload)


@dataclass(frozen=True, slots=True)
class CheckpointRequestRecord:
    """One immutable request bound to an exact paused execution state."""

    schema_version: int
    checkpoint_id: str
    project_id: str
    run_id: str
    code: str
    message: str
    node_id: str
    pending_resume_node: str
    state_version: int
    state_digest: str
    package_digest: str
    write_inputs_digest: str
    source_revision: str
    baseline_head: str
    baseline_tree_id: str
    capability_fingerprint: str
    risk_level: RiskLevel
    operations_digest: str
    nonce: str
    created_at: str
    expires_at: str
    request_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not _CHECKPOINT_ID.fullmatch(self.checkpoint_id):
            raise CheckpointEvidenceError("invalid checkpoint request identity")
        text = (
            self.project_id,
            self.run_id,
            self.code,
            self.message,
            self.node_id,
            self.pending_resume_node,
            self.state_digest,
            self.package_digest,
            self.write_inputs_digest,
            self.source_revision,
            self.baseline_head,
            self.baseline_tree_id,
            self.capability_fingerprint,
            self.operations_digest,
            self.nonce,
        )
        if any(not isinstance(value, str) or not value or "\x00" in value for value in text):
            raise CheckpointEvidenceError("invalid checkpoint request field")
        if type(self.state_version) is not int or self.state_version < 0:
            raise CheckpointEvidenceError("invalid checkpoint state version")
        if not isinstance(self.risk_level, RiskLevel):
            raise CheckpointEvidenceError("invalid checkpoint risk level")
        created = parse_timestamp(self.created_at)
        expires = parse_timestamp(self.expires_at)
        if expires <= created:
            raise CheckpointEvidenceError("invalid checkpoint expiry")
        if self.request_digest != _digest_without(self, "request_digest"):
            raise CheckpointEvidenceError("checkpoint request digest mismatch")

    @classmethod
    def create(cls, **values: object) -> CheckpointRequestRecord:
        return cls(**values, request_digest=sha256_digest(values))


@dataclass(frozen=True, slots=True)
class CheckpointDecision:
    """One immutable, actor-attributed, nonce-bound checkpoint decision."""

    schema_version: int
    checkpoint_id: str
    request_digest: str
    nonce: str
    outcome: CheckpointOutcome
    actor: str
    decided_at: str
    decision_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not _CHECKPOINT_ID.fullmatch(self.checkpoint_id):
            raise CheckpointEvidenceError("invalid checkpoint decision identity")
        if not self.request_digest or not self.nonce:
            raise CheckpointEvidenceError("invalid checkpoint decision binding")
        if not isinstance(self.outcome, CheckpointOutcome):
            raise CheckpointEvidenceError("invalid checkpoint outcome")
        if (
            not isinstance(self.actor, str)
            or not self.actor.strip()
            or "\x00" in self.actor
            or len(self.actor) > MAX_ACTOR_LENGTH
        ):
            raise CheckpointEvidenceError("checkpoint actor is invalid")
        parse_timestamp(self.decided_at)
        if self.decision_digest != _digest_without(self, "decision_digest"):
            raise CheckpointEvidenceError("checkpoint decision digest mismatch")

    @classmethod
    def create(cls, **values: object) -> CheckpointDecision:
        return cls(**values, decision_digest=sha256_digest(values))


class CheckpointStore:
    """Atomically create and strictly load checkpoint request/decision records."""

    def __init__(
        self,
        run_path: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.run_path = Path(run_path)
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(32))

    def new_nonce(self) -> str:
        nonce = self.nonce_factory()
        if not isinstance(nonce, str) or not nonce or "\x00" in nonce:
            raise CheckpointStoreError("checkpoint nonce factory returned an invalid token")
        return nonce

    def request_path(self, checkpoint_id: str) -> Path:
        return self._directory(checkpoint_id) / "request.json"

    def decision_path(self, checkpoint_id: str) -> Path:
        return self._directory(checkpoint_id) / "decision.json"

    def load_request(self, checkpoint_id: str) -> CheckpointRequestRecord | None:
        return self._load(self.request_path(checkpoint_id), CheckpointRequestRecord)

    def load_decision(self, checkpoint_id: str) -> CheckpointDecision | None:
        return self._load(self.decision_path(checkpoint_id), CheckpointDecision)

    def write_request_once(self, record: CheckpointRequestRecord) -> CheckpointRequestRecord:
        path = self.request_path(record.checkpoint_id)
        with AdvisoryFileLock(path.with_suffix(".lock"), blocking=True):
            existing = self._load(path, CheckpointRequestRecord)
            if existing is not None:
                return existing
            atomic_write_bytes(path, canonical_json_bytes(encode_value(record)))
        return record

    def write_decision_once(self, decision: CheckpointDecision) -> None:
        path = self.decision_path(decision.checkpoint_id)
        with AdvisoryFileLock(path.with_suffix(".lock"), blocking=True):
            if path.exists():
                raise CheckpointStoreError("checkpoint_already_decided")
            atomic_write_bytes(path, canonical_json_bytes(encode_value(decision)))

    def relative_decision_reference(self, checkpoint_id: str) -> str:
        self._directory(checkpoint_id)
        return f"checkpoints/{checkpoint_id}/decision.json"

    def _directory(self, checkpoint_id: str) -> Path:
        if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise CheckpointStoreError("invalid checkpoint ID")
        root = (self.run_path / "checkpoints").resolve()
        candidate = (root / checkpoint_id).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise CheckpointStoreError("checkpoint path escapes run storage") from exc
        return candidate

    @staticmethod
    def _load(path: Path, kind: type[CheckpointRequestRecord] | type[CheckpointDecision]):
        if not path.exists():
            return None
        try:
            return decode_value(parse_json_bytes(path.read_bytes()), kind)
        except (OSError, SerializationError, CheckpointEvidenceError, ValueError) as exc:
            raise CheckpointEvidenceError("checkpoint evidence is invalid") from exc
