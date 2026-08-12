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
