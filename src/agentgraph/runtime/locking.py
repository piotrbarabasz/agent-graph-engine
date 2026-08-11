"""Cross-platform OS advisory locking with diagnostic lease metadata."""

from __future__ import annotations

import hashlib
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from .atomic import atomic_write_bytes
from .codec import (
    canonical_json_bytes,
    decode_value,
    format_timestamp,
    parse_json_bytes,
    parse_timestamp,
    utc_now,
)
from .errors import (
    ProjectLockedError,
    SerializationError,
    StaleLeaseError,
    StaleLeaseMismatchError,
    UnsupportedSchemaError,
)


class AdvisoryFileLock:
    """One-byte non-blocking exclusive OS advisory lock."""

    def __init__(self, path: Path, *, blocking: bool = False) -> None:
        self.path = path
        self.blocking = blocking
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.seek(0)
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                msvcrt.locking(stream.fileno(), mode, 1)
            else:
                import fcntl

                mode = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(stream.fileno(), mode)
        except OSError as exc:
            stream.close()
            raise ProjectLockedError(f"lock is already held: {self.path}") from exc
        self._stream = stream

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> AdvisoryFileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class LockMetadata:
    """Diagnostic evidence associated with an active project lock."""

    project_id: str
    run_id: str
    pid: int
    hostname: str
    acquired_at: str
    heartbeat_at: str
    engine_version: str = "0.1.0"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise UnsupportedSchemaError("unsupported lock metadata schema")
        if not self.project_id.startswith("prj_") or not self.run_id.startswith("run_"):
            raise SerializationError("invalid lock metadata identity")
        parse_timestamp(self.acquired_at)
        parse_timestamp(self.heartbeat_at)


class ProjectLock:
    """Project-wide writer lock; lock.json is evidence, never the primitive."""

    def __init__(
        self,
        lock_path: Path,
        metadata_path: Path,
        *,
        project_id: str,
        run_id: str,
        recovery: bool = False,
        now: Callable[[], datetime] = utc_now,
        recovery_evidence_dir: Path | None = None,
    ) -> None:
        self.os_lock = AdvisoryFileLock(lock_path)
        self.metadata_path = metadata_path
        self.project_id = project_id
        self.run_id = run_id
        self.recovery = recovery
        self.now = now
        self.recovery_evidence_dir = recovery_evidence_dir
        self.metadata: LockMetadata | None = None

    def acquire(self) -> LockMetadata:
        self.os_lock.acquire()
        try:
            if self.metadata_path.exists():
                if not self.recovery:
                    raise StaleLeaseError("stale lock metadata requires explicit recovery mode")
                raw_lease = self.metadata_path.read_bytes()
                try:
                    stale = decode_value(parse_json_bytes(raw_lease), LockMetadata)
                except (OSError, SerializationError) as exc:
                    raise StaleLeaseMismatchError("stale lease metadata is invalid") from exc
                if stale.project_id != self.project_id or stale.run_id != self.run_id:
                    raise StaleLeaseMismatchError(
                        "stale lease identity does not match requested recovery run"
                    )
                evidence_dir = self.recovery_evidence_dir or self.metadata_path.parent / "recovery"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(raw_lease).hexdigest()
                atomic_write_bytes(evidence_dir / f"stale-lease-{digest[:16]}.json", raw_lease)
            timestamp = format_timestamp(self.now())
            self.metadata = LockMetadata(
                project_id=self.project_id,
                run_id=self.run_id,
                pid=os.getpid(),
                hostname=socket.gethostname(),
                acquired_at=timestamp,
                heartbeat_at=timestamp,
            )
            atomic_write_bytes(self.metadata_path, canonical_json_bytes(self.metadata))
            return self.metadata
        except BaseException:
            self.os_lock.release()
            raise

    def heartbeat(self) -> None:
        if self.metadata is None:
            raise ProjectLockedError("project lock is not held")
        self.metadata = LockMetadata(
            project_id=self.metadata.project_id,
            run_id=self.metadata.run_id,
            pid=self.metadata.pid,
            hostname=self.metadata.hostname,
            acquired_at=self.metadata.acquired_at,
            heartbeat_at=format_timestamp(self.now()),
            engine_version=self.metadata.engine_version,
        )
        atomic_write_bytes(self.metadata_path, canonical_json_bytes(self.metadata))

    def release(self) -> None:
        try:
            if self.metadata is not None:
                self._remove_metadata()
        finally:
            self.metadata = None
            self.os_lock.release()

    def _remove_metadata(self) -> None:
        self.metadata_path.unlink(missing_ok=True)

    def read_metadata(self) -> dict[str, object]:
        return parse_json_bytes(self.metadata_path.read_bytes())

    def __enter__(self) -> ProjectLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
