"""Neutral shell-free process and local Git infrastructure."""

from .git import (
    DiffCheckResult,
    GitAdapter,
    GitCommitIdentity,
    GitCommitResult,
    GitRepository,
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
    "DiffCheckResult",
    "GitAdapter",
    "GitCommitIdentity",
    "GitCommitResult",
    "GitRepository",
    "GitWorktreeResult",
    "ProcessRunner",
    "ProcessStatus",
    "ProcessTermination",
    "Redactor",
    "RepositorySnapshot",
]
