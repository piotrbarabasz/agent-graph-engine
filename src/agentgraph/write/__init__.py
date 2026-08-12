"""Public API for the first controlled local-write vertical slice."""

from .apply import apply_changeset
from .capability import (
    capability_fingerprint,
    path_is_allowed,
    reconcile_write_capability,
    stable_path_union,
)
from .errors import (
    ChangePathError,
    ChangeSetError,
    CommitVerificationError,
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
    FileChange,
    WriteSliceIssue,
    WriteSliceOutcome,
    WriteSliceReport,
    WriteSliceRequest,
)
from .provider import ChangeProvider

__all__ = [
    "AppliedChangeSet",
    "AppliedFile",
    "ChangePathError",
    "ChangeProvider",
    "ChangeRequest",
    "ChangeSet",
    "ChangeSetError",
    "CommitVerificationError",
    "FileChange",
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
