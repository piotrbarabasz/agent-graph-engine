"""Typed failures for neutral process and local Git infrastructure."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .receipts import CommandReceipt


class AgentGraphInfraError(Exception):
    """Base error for local infrastructure primitives."""


class ProcessError(AgentGraphInfraError):
    """Base process execution error."""


class InvalidCommandSpecError(ProcessError):
    """A command specification violates the structural execution contract."""


class InvalidCommandIdentifierError(ProcessError):
    """A command identifier is malformed or unsafe."""


class ProcessStartError(ProcessError):
    """The operating system could not start a requested executable."""

    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__(f"failed to start command {command_id}")


class ProcessOutputError(ProcessError):
    """Captured process output could not be read safely."""


class GitError(AgentGraphInfraError):
    """Base local Git adapter error."""


class GitUnavailableError(GitError):
    """The configured Git executable could not be started."""


class NotAGitRepositoryError(GitError):
    """A path is not inside a readable local Git repository."""


class GitCommandError(GitError):
    """A local Git command failed with a redacted diagnostic receipt."""

    def __init__(self, message: str, receipt: CommandReceipt) -> None:
        self.receipt = receipt
        super().__init__(
            f"{message} (command_id={receipt.command_id}, status={receipt.status.value}, "
            f"exit_code={receipt.exit_code}, stderr={receipt.stderr_preview!r})"
        )


class GitOutputError(GitError):
    """Machine-readable Git output is truncated or malformed."""


class InvalidGitReferenceError(GitError):
    """Git rejected a branch or revision name."""


class GitPathError(GitError):
    """A Git mutation path is empty, ambiguous, or escapes the repository."""


class InvalidGitOperationError(GitError):
    """A local Git mutation request is structurally invalid."""


class NothingToCommitError(GitError):
    """A commit was requested without any staged changes."""
