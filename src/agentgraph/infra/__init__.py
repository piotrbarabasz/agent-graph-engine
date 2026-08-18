"""Neutral shell-free process and local Git infrastructure."""

from .git import (
    CommitDiffCheckResult,
    DiffCheckResult,
    GitAdapter,
    GitCommitIdentity,
    GitCommitResult,
    GitPushResult,
    GitRepository,
    GitTreeEntry,
    GitWorktreeResult,
    RepositorySnapshot,
)
from .process import CancellationToken, CommandSpec, ProcessRunner
from .receipts import CommandReceipt, CommandResult, ProcessStatus, ProcessTermination
from .redaction import Redactor

__all__ = [
    "CancellationToken",
    "CommandReceipt",
    "CommandResult",
    "CommandSpec",
    "CommitDiffCheckResult",
    "DiffCheckResult",
    "GitAdapter",
    "GitCommitIdentity",
    "GitCommitResult",
    "GitPushResult",
    "GitRepository",
    "GitTreeEntry",
    "GitWorktreeResult",
    "ProcessRunner",
    "ProcessStatus",
    "ProcessTermination",
    "Redactor",
    "RepositorySnapshot",
]
