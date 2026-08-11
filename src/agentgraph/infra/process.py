"""Shell-free synchronous process execution with bounded deadlock-safe capture."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO

from .errors import InvalidCommandSpecError, ProcessOutputError, ProcessStartError
from .receipts import (
    CommandReceipt,
    CommandResult,
    ProcessStatus,
    ProcessTermination,
    generate_command_id,
    validate_command_id,
)
from .redaction import Redactor, is_sensitive_environment_key

DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Validated structural specification for one shell-free command."""

    argv: tuple[str, ...] = field(repr=False)
    cwd: Path = field(repr=False)
    timeout_seconds: float | None = None
    env: Mapping[str, str] | None = field(default=None, repr=False)
    stdin: bytes | None = field(default=None, repr=False)
    max_stdout_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    termination_grace_seconds: float = 1.0
    secret_values: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if type(self.argv) is not tuple or not self.argv:
            raise InvalidCommandSpecError("argv must be a non-empty tuple")
        if not all(type(item) is str and "\x00" not in item for item in self.argv):
            raise InvalidCommandSpecError("argv elements must be strings without NUL")
        if not self.argv[0]:
            raise InvalidCommandSpecError("argv executable must not be empty")
        cwd = Path(self.cwd).expanduser().resolve()
        if not cwd.is_dir():
            raise InvalidCommandSpecError("cwd must exist and be a directory")
        object.__setattr__(self, "cwd", cwd)
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0
        ):
            raise InvalidCommandSpecError("timeout_seconds must be positive")
        if isinstance(self.termination_grace_seconds, bool) or self.termination_grace_seconds <= 0:
            raise InvalidCommandSpecError("termination_grace_seconds must be positive")
        for limit in (self.max_stdout_bytes, self.max_stderr_bytes):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise InvalidCommandSpecError("output limits must be positive integers")
        if self.stdin is not None and type(self.stdin) is not bytes:
            raise InvalidCommandSpecError("stdin must be bytes or None")
        if type(self.secret_values) is not tuple or not all(
            type(value) is str for value in self.secret_values
        ):
            raise InvalidCommandSpecError("secret_values must be a tuple of strings")
        if self.env is not None:
            if not isinstance(self.env, Mapping):
                raise InvalidCommandSpecError("env must be a string mapping or None")
            overrides = dict(self.env)
            if not all(
                type(key) is str
                and key
                and "=" not in key
                and "\x00" not in key
                and type(value) is str
                and "\x00" not in value
                for key, value in overrides.items()
            ):
                raise InvalidCommandSpecError("env keys and values must be valid strings")
            object.__setattr__(self, "env", MappingProxyType(overrides))


class CancellationToken:
    """Small thread-safe cooperative cancellation signal."""

    __slots__ = ("_event",)

    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event or threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class ProcessRunner:
    """Execute independent commands without a shell and always reap started children."""

    def __init__(
        self,
        *,
        command_id_factory: Callable[[], str] = generate_command_id,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_interval_seconds: float = 0.01,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.command_id_factory = command_id_factory
        self.monotonic = monotonic
        self.now = now
        self.poll_interval_seconds = poll_interval_seconds

    def run(
        self,
        spec: CommandSpec,
        *,
        cancellation: CancellationToken | None = None,
    ) -> CommandResult:
        """Run one command synchronously and return bounded raw output plus a receipt."""

        if not isinstance(spec, CommandSpec):
            raise InvalidCommandSpecError("run requires a CommandSpec")
        command_id = validate_command_id(self.command_id_factory())
        started_at = self.now()
        started_tick = self.monotonic()
        environment = os.environ.copy()
        if spec.env:
            environment.update(spec.env)
        redactor = self._redactor_for(spec, environment)
        if cancellation is not None and cancellation.is_cancelled():
            return self._cancelled_before_start(
                spec, command_id, started_at, started_tick, redactor
            )

        with ExitStack() as stack:
            stdout_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
            stderr_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
            stdin_source: BinaryIO | int
            if spec.stdin is None:
                stdin_source = subprocess.DEVNULL
            else:
                stdin_file = stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
                stdin_file.write(spec.stdin)
                stdin_file.seek(0)
                stdin_source = stdin_file
            try:
                process = subprocess.Popen(
                    spec.argv,
                    cwd=spec.cwd,
                    env=environment,
                    stdin=stdin_source,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=os.name != "nt",
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                )
            except (OSError, ValueError) as exc:
                raise ProcessStartError(command_id) from exc

            status, termination = self._monitor(process, spec, started_tick, cancellation)
            try:
                stdout, stdout_size, stdout_truncated = self._read_bounded(
                    stdout_file, spec.max_stdout_bytes
                )
                stderr, stderr_size, stderr_truncated = self._read_bounded(
                    stderr_file, spec.max_stderr_bytes
                )
            except OSError as exc:
                raise ProcessOutputError(f"failed to read output for {command_id}") from exc

        finished_tick = self.monotonic()
        finished_at = self.now()
        receipt = self._receipt(
            spec=spec,
            command_id=command_id,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=max(0, round((finished_tick - started_tick) * 1000)),
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_size=stdout_size,
            stderr_size=stderr_size,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            termination=termination,
            redactor=redactor,
        )
        return CommandResult(receipt, stdout, stderr)

    def _monitor(
        self,
        process: subprocess.Popen[bytes],
        spec: CommandSpec,
        started_tick: float,
        cancellation: CancellationToken | None,
    ) -> tuple[ProcessStatus, ProcessTermination]:
        deadline = None if spec.timeout_seconds is None else started_tick + spec.timeout_seconds
        try:
            while (exit_code := process.poll()) is None:
                if cancellation is not None and cancellation.is_cancelled():
                    termination = self._terminate(process, spec.termination_grace_seconds)
                    return ProcessStatus.CANCELLED, termination
                now = self.monotonic()
                if deadline is not None and now >= deadline:
                    termination = self._terminate(process, spec.termination_grace_seconds)
                    return ProcessStatus.TIMED_OUT, termination
                remaining = None if deadline is None else max(0.0, deadline - now)
                delay = (
                    self.poll_interval_seconds
                    if remaining is None
                    else min(self.poll_interval_seconds, remaining)
                )
                time.sleep(delay)
            process.wait()
            status = ProcessStatus.SUCCEEDED if exit_code == 0 else ProcessStatus.FAILED
            return status, ProcessTermination.NONE
        except BaseException:
            if process.poll() is None:
                self._terminate(process, spec.termination_grace_seconds)
            else:
                process.wait()
            raise

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> ProcessTermination:
        if process.poll() is not None:
            process.wait()
            return ProcessTermination.GRACEFUL
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait()
            return ProcessTermination.GRACEFUL
        try:
            process.wait(timeout=grace_seconds)
            return ProcessTermination.GRACEFUL
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return ProcessTermination.FORCED

    @staticmethod
    def _read_bounded(stream: BinaryIO, limit: int) -> tuple[bytes, int, bool]:
        stream.flush()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size <= limit:
            stream.seek(0)
            return stream.read(), size, False
        head_size = limit // 2
        tail_size = limit - head_size
        stream.seek(0)
        head = stream.read(head_size)
        stream.seek(-tail_size, os.SEEK_END)
        tail = stream.read(tail_size)
        return head + tail, size, True

    def _cancelled_before_start(
        self,
        spec: CommandSpec,
        command_id: str,
        started_at: datetime,
        started_tick: float,
        redactor: Redactor,
    ) -> CommandResult:
        receipt = self._receipt(
            spec=spec,
            command_id=command_id,
            started_at=started_at,
            finished_at=self.now(),
            duration_ms=max(0, round((self.monotonic() - started_tick) * 1000)),
            status=ProcessStatus.CANCELLED,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            stdout_size=0,
            stderr_size=0,
            stdout_truncated=False,
            stderr_truncated=False,
            termination=ProcessTermination.NOT_STARTED,
            redactor=redactor,
        )
        return CommandResult(receipt, b"", b"")

    @staticmethod
    def _redactor_for(spec: CommandSpec, environment: Mapping[str, str]) -> Redactor:
        environment_secrets = (
            value for key, value in environment.items() if is_sensitive_environment_key(key)
        )
        return Redactor((*spec.secret_values, *environment_secrets))

    @staticmethod
    def _receipt(
        *,
        spec: CommandSpec,
        command_id: str,
        started_at: datetime,
        finished_at: datetime,
        duration_ms: int,
        status: ProcessStatus,
        exit_code: int | None,
        stdout: bytes,
        stderr: bytes,
        stdout_size: int,
        stderr_size: int,
        stdout_truncated: bool,
        stderr_truncated: bool,
        termination: ProcessTermination,
        redactor: Redactor,
    ) -> CommandReceipt:
        return CommandReceipt(
            command_id=command_id,
            argv=redactor.redact_argv(spec.argv),
            cwd=redactor.redact_text(str(spec.cwd)),
            started_at=_format_timestamp(started_at),
            finished_at=_format_timestamp(finished_at),
            duration_ms=duration_ms,
            status=status,
            exit_code=exit_code,
            stdout_size=stdout_size,
            stderr_size=stderr_size,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            termination=termination,
            stdout_preview=redactor.redact_bytes_preview(stdout),
            stderr_preview=redactor.redact_bytes_preview(stderr),
            env_overrides=redactor.redact_environment(spec.env or {}),
        )


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ProcessRunner now() must return an aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
