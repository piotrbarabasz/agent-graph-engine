"""Immutable typed process results and redacted command receipts."""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .errors import InvalidCommandIdentifierError

MAX_COMMAND_ID_LENGTH = 128
_COMMAND_ID_PATTERN = re.compile(r"cmd_[A-Za-z0-9_-]+", re.ASCII)


def validate_command_id(command_id: str) -> str:
    """Validate and return an opaque filesystem-safe command identity."""

    if (
        type(command_id) is not str
        or len(command_id) > MAX_COMMAND_ID_LENGTH
        or _COMMAND_ID_PATTERN.fullmatch(command_id) is None
    ):
        raise InvalidCommandIdentifierError("invalid command_id")
    return command_id


def generate_command_id() -> str:
    """Generate an opaque command identifier suitable for receipts and filenames."""

    return validate_command_id(f"cmd_{secrets.token_urlsafe(20)}")


class ProcessStatus(StrEnum):
    """Semantic outcome of one child-process invocation."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class ProcessTermination(StrEnum):
    """Termination action performed by ProcessRunner."""

    NONE = "none"
    NOT_STARTED = "not_started"
    GRACEFUL = "graceful"
    FORCED = "forced"


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Versioned redacted diagnostic evidence for one command invocation."""

    command_id: str
    argv: tuple[str, ...]
    cwd: str
    started_at: str
    finished_at: str
    duration_ms: int
    status: ProcessStatus
    exit_code: int | None
    stdout_size: int
    stderr_size: int
    stdout_truncated: bool
    stderr_truncated: bool
    termination: ProcessTermination
    stdout_preview: str = ""
    stderr_preview: str = ""
    env_overrides: tuple[tuple[str, str], ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        validate_command_id(self.command_id)
        if self.schema_version != 1:
            raise ValueError("unsupported command receipt schema")
        if self.duration_ms < 0 or self.stdout_size < 0 or self.stderr_size < 0:
            raise ValueError("command receipt sizes and duration must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        """Return a plain JSON-compatible representation."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Raw bounded child output paired with its safe diagnostic receipt."""

    receipt: CommandReceipt
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)

    def stdout_text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self.stdout.decode(encoding, errors)

    def stderr_text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self.stderr.decode(encoding, errors)
