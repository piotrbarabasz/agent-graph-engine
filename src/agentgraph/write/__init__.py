"""Public API for the first controlled local-write vertical slice."""

from .apply import apply_changeset
from .capability import (
    capability_fingerprint,
    normalize_repo_path,
    path_is_allowed,
    reconcile_write_capability,
    stable_path_union,
)
from .errors import (
    ChangePathError,
    ChangeProviderBlockedError,
    ChangeSetError,
    CommitVerificationError,
    PostCommitRecoveryRequired,
    ProviderMutationError,
    StaleFileError,
    UnsupportedWriteScopeError,
    ValidationExecutionError,
    WorkCapabilityMismatchError,
    WorkspaceError,
    WriteBaselineDriftError,
    WritePreparationError,
    WriteSliceError,
)
from .models import (
    AppliedChangeSet,
    AppliedFile,
    ChangeRequest,
    ChangeSet,
    CommitWitness,
    FileChange,
    WriteSliceIssue,
    WriteSliceOutcome,
    WriteSliceReport,
    WriteSliceRequest,
)
from .provider import ChangeProvider, ChangeProviderContext

__all__ = [
    "AppliedChangeSet",
    "AppliedFile",
    "ChangePathError",
    "ChangeProvider",
    "ChangeProviderBlockedError",
    "ChangeProviderContext",
    "ChangeRequest",
    "ChangeSet",
    "ChangeSetError",
    "CommitVerificationError",
    "CommitWitness",
    "FileChange",
    "PostCommitRecoveryRequired",
    "ProviderMutationError",
    "StaleFileError",
    "UnsupportedWriteScopeError",
    "ValidationExecutionError",
    "WorkCapabilityMismatchError",
    "WorkspaceError",
    "WriteBaselineDriftError",
    "WritePreparationError",
    "WriteSliceError",
    "WriteSliceIssue",
    "WriteSliceOutcome",
    "WriteSliceReport",
    "WriteSliceRequest",
    "WriteSliceRunner",
    "apply_changeset",
    "capability_fingerprint",
    "normalize_repo_path",
    "path_is_allowed",
    "reconcile_write_capability",
    "stable_path_union",
]


def __getattr__(name: str):
    """Load the orchestrator lazily so node contracts do not form an import cycle."""

    if name == "WriteSliceRunner":
        from .runner import WriteSliceRunner

        return WriteSliceRunner
    raise AttributeError(name)
