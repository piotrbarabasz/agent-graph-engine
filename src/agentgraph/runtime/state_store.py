"""Integrity-checked atomic GraphState persistence with CAS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentgraph.core import AgentGraphError, GraphState

from .atomic import atomic_write_bytes
from .codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
    parse_json_bytes,
    sha256_digest,
)
from .errors import (
    SerializationError,
    StateConflictError,
    StateCorruptionError,
    StateStoreError,
    UnsupportedSchemaError,
)
from .locking import AdvisoryFileLock


@dataclass(frozen=True, slots=True)
class PersistedState:
    """State snapshot plus its canonical integrity digest."""

    state: GraphState
    digest: str


class StateStore:
    """Persist one run's current state in an atomic versioned envelope."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def digest_for_state(state: GraphState) -> str:
        """Return the canonical digest recorded in state and journal envelopes."""

        return sha256_digest(encode_value(state))

    def initialize(self, state: GraphState) -> PersistedState:
        """Create state.json once without overwriting an existing run state."""

        with AdvisoryFileLock(self.path.with_suffix(".lock"), blocking=True):
            if self.path.exists():
                raise StateStoreError("state store is already initialized")
            return self._write(state)

    def load_persisted(self) -> PersistedState:
        """Load, authenticate, and strictly decode state.json."""

        try:
            envelope = parse_json_bytes(self.path.read_bytes())
            if not isinstance(envelope, dict) or set(envelope) != {
                "store_schema_version",
                "state_digest",
                "state",
            }:
                raise StateCorruptionError("invalid state envelope fields")
            if envelope["store_schema_version"] != 1:
                raise UnsupportedSchemaError("unsupported state store schema")
            if not isinstance(envelope["state_digest"], str):
                raise StateCorruptionError("state digest must be a string")
            computed = sha256_digest(envelope["state"])
            if computed != envelope["state_digest"]:
                raise StateCorruptionError("state digest mismatch")
            state = decode_value(envelope["state"], GraphState)
            return PersistedState(state, computed)
        except UnsupportedSchemaError:
            raise
        except StateCorruptionError:
            raise
        except (OSError, SerializationError, AgentGraphError, TypeError, ValueError) as exc:
            raise StateCorruptionError("persisted state is corrupt") from exc

    def load(self) -> GraphState:
        """Return the authenticated GraphState snapshot."""

        return self.load_persisted().state

    def digest(self) -> str:
        """Return the authenticated current state digest."""

        return self.load_persisted().digest

    def compare_and_swap(
        self, expected_state_version: int, next_state: GraphState
    ) -> PersistedState:
        """Atomically persist exactly N+1 when the current version is exactly N."""

        with AdvisoryFileLock(self.path.with_suffix(".lock"), blocking=True):
            current = self.load()
            if current.state_version != expected_state_version:
                raise StateConflictError(
                    f"expected state version {expected_state_version}, "
                    f"found {current.state_version}"
                )
            if next_state.state_version != expected_state_version + 1:
                raise StateConflictError("next state must be exactly expected version + 1")
            return self._write(next_state)

    def _write(self, state: GraphState) -> PersistedState:
        encoded = encode_value(state)
        digest = sha256_digest(encoded)
        envelope = {
            "store_schema_version": 1,
            "state_digest": digest,
            "state": encoded,
        }
        atomic_write_bytes(self.path, canonical_json_bytes(envelope))
        return PersistedState(state, digest)
