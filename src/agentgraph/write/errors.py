"""Typed failures at the controlled local-write boundary."""


class WriteSliceError(Exception):
    """Base class for M006 write-slice failures."""


class WritePreparationError(WriteSliceError):
    pass


class WorkCapabilityMismatchError(WritePreparationError):
    pass


class UnsupportedWriteScopeError(WritePreparationError):
    pass


class ChangeSetError(WriteSliceError):
    pass


class ChangeProviderBlockedError(WriteSliceError):
    """Expected provider inability that blocks the graph without a retry."""

    code = "change_provider_blocked"

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        self.message = message
        super().__init__(message)


class ProviderMutationError(WriteSliceError):
    code = "provider_mutated_repository"


class ChangePathError(ChangeSetError):
    pass


class StaleFileError(ChangeSetError):
    pass


class WorkspaceError(WriteSliceError):
    pass


class WriteBaselineDriftError(WriteSliceError):
    pass


class ValidationExecutionError(WriteSliceError):
    pass


class CommitVerificationError(WriteSliceError):
    pass


class PostCommitRecoveryRequired(WriteSliceError):
    """A commit exists or may exist, so normal node failure is unsafe."""
