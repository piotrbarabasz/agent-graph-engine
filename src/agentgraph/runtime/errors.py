"""Runtime persistence, locking, and recovery errors."""


class AgentGraphRuntimeError(Exception):
    """Base error for the local durable runtime."""


class RuntimePathError(AgentGraphRuntimeError):
    """A runtime path is invalid or unsafe."""


class InvalidRuntimeIdentifierError(RuntimePathError):
    """A runtime identifier is malformed or unsafe for path construction."""


class SerializationError(AgentGraphRuntimeError):
    """JSON data violates a strict serialization contract."""


class UnsupportedSchemaError(SerializationError):
    """A persisted schema version is unsupported."""


class ProjectRegistryError(AgentGraphRuntimeError):
    """The project registry is invalid or inconsistent."""


class ProjectLockedError(AgentGraphRuntimeError):
    """Another writer holds the project OS lock."""


class StaleLeaseError(ProjectLockedError):
    """Diagnostic lease metadata remains and explicit recovery is required."""


class StaleLeaseMismatchError(StaleLeaseError):
    """Stale lease identity does not match the requested recovery run."""


class ActiveRunExistsError(AgentGraphRuntimeError):
    """A project already owns an unfinished writer run."""


class RunAlreadyExistsError(AgentGraphRuntimeError):
    """A run directory already exists."""


class RunNotFoundError(AgentGraphRuntimeError):
    """A requested run does not exist."""


class IncompleteRunInitializationError(AgentGraphRuntimeError):
    """Staging or canonical run initialization is incomplete."""


class StateStoreError(AgentGraphRuntimeError):
    """Base state persistence error."""


class StateConflictError(StateStoreError):
    """A compare-and-swap expected version is stale or premature."""


class StateCorruptionError(StateStoreError):
    """Persisted state failed integrity or strict decoding."""


class JournalError(AgentGraphRuntimeError):
    """Base append-only journal error."""


class JournalCorruptionError(JournalError):
    """A complete journal record or checksum chain is corrupt."""


class TruncatedJournalError(JournalError):
    """The final journal tail is incomplete and requires explicit repair."""

    def __init__(self, message: str, *, raw_tail: bytes, valid_bytes: int) -> None:
        super().__init__(message)
        self.raw_tail = raw_tail
        self.valid_bytes = valid_bytes


class RecoveryError(AgentGraphRuntimeError):
    """A recovery operation cannot be completed safely."""


class CheckpointStoreError(AgentGraphRuntimeError):
    """Durable checkpoint evidence could not be safely read or written."""


class CheckpointEvidenceError(CheckpointStoreError):
    """Checkpoint evidence is malformed, corrupt, or internally inconsistent."""
