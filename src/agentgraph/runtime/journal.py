"""Append-only checksummed durable protocol journal."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .atomic import atomic_write_bytes
from .codec import (
    canonical_json_bytes,
    decode_value,
    format_timestamp,
    parse_json_bytes,
    parse_timestamp,
    sha256_digest,
    utc_now,
)
from .errors import (
    InvalidRuntimeIdentifierError,
    JournalCorruptionError,
    SerializationError,
    TruncatedJournalError,
    UnsupportedSchemaError,
)
from .ids import generate_record_id, validate_record_id, validate_run_id


class JournalRecordType(StrEnum):
    """Typed records in the durable graph-step protocol."""

    RUN_STARTED = "RUN_STARTED"
    NODE_STARTED = "NODE_STARTED"
    NODE_RESULT_RECORDED = "NODE_RESULT_RECORDED"
    TRANSITION_COMMITTED = "TRANSITION_COMMITTED"
    RUN_FINALIZED = "RUN_FINALIZED"
    RECOVERY_NOTE = "RECOVERY_NOTE"


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """One authenticated record in a run's checksum chain."""

    seq: int
    record_id: str
    record_type: JournalRecordType
    run_id: str
    timestamp: str
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    previous_checksum: str | None = None
    checksum: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.seq < 1:
            raise JournalCorruptionError("invalid journal record schema or sequence")
        validate_record_id(self.record_id)
        validate_run_id(self.run_id)
        parse_timestamp(self.timestamp)
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class Journal:
    """Append and authenticate one run's JSONL protocol history."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        *,
        record_id_factory: Callable[[], str] = generate_record_id,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        validate_run_id(run_id)
        self.path = path
        self.run_id = run_id
        self.record_id_factory = record_id_factory
        self.now = now

    def initialize(self) -> None:
        """Create an empty journal without replacing existing history."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def append(self, record_type: JournalRecordType, payload: Mapping[str, Any]) -> JournalRecord:
        """Append and fsync the next typed checksum-linked record."""

        records = self.load()
        previous = records[-1].checksum if records else None
        content = {
            "schema_version": 1,
            "seq": len(records) + 1,
            "record_id": validate_record_id(self.record_id_factory()),
            "record_type": record_type.value,
            "run_id": self.run_id,
            "timestamp": format_timestamp(self.now()),
            "payload": dict(payload),
            "previous_checksum": previous,
        }
        content["checksum"] = sha256_digest(content)
        record = decode_value(content, JournalRecord)
        with self.path.open("ab") as stream:
            stream.write(canonical_json_bytes(content) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def load(self) -> tuple[JournalRecord, ...]:
        """Validate every sequence, run ID, checksum, and chain link."""

        if not self.path.exists():
            return ()
        raw = self.path.read_bytes()
        lines = raw.splitlines(keepends=True)
        records: list[JournalRecord] = []
        consumed = 0
        for index, line in enumerate(lines):
            is_last = index == len(lines) - 1
            if is_last and not line.endswith(b"\n"):
                raise TruncatedJournalError(
                    "journal has an incomplete final tail",
                    raw_tail=line,
                    valid_bytes=consumed,
                )
            serialized = line[:-1]
            try:
                data = parse_json_bytes(serialized)
                record = decode_value(data, JournalRecord)
                self._validate_record(record, data, records)
            except (
                SerializationError,
                InvalidRuntimeIdentifierError,
                JournalCorruptionError,
                UnsupportedSchemaError,
            ) as exc:
                raise JournalCorruptionError(f"corrupt journal record {index + 1}") from exc
            records.append(record)
            consumed += len(line)
        return tuple(records)

    def _validate_record(
        self,
        record: JournalRecord,
        data: Mapping[str, Any],
        previous: list[JournalRecord],
    ) -> None:
        if record.run_id != self.run_id:
            raise JournalCorruptionError("journal run ID mismatch")
        if record.seq != len(previous) + 1:
            raise JournalCorruptionError("journal sequence gap or duplicate")
        expected_previous = previous[-1].checksum if previous else None
        if record.previous_checksum != expected_previous:
            raise JournalCorruptionError("broken previous checksum chain")
        unsigned = dict(data)
        checksum = unsigned.pop("checksum", None)
        if checksum != sha256_digest(unsigned):
            raise JournalCorruptionError("journal checksum mismatch")

    def repair_truncated_tail(self, recovery_dir: Path) -> JournalRecord:
        """Preserve an incomplete tail, truncate under caller lock, and record recovery."""

        try:
            self.load()
        except TruncatedJournalError as error:
            recovery_dir.mkdir(parents=True, exist_ok=True)
            tail_digest = f"sha256:{hashlib.sha256(error.raw_tail).hexdigest()}"
            evidence_id = validate_record_id(self.record_id_factory())
            atomic_write_bytes(recovery_dir / f"{evidence_id}.tail", error.raw_tail)
            metadata = {
                "schema_version": 1,
                "run_id": self.run_id,
                "tail_digest": tail_digest,
                "tail_size": len(error.raw_tail),
                "preserved_at": format_timestamp(self.now()),
            }
            atomic_write_bytes(recovery_dir / f"{evidence_id}.json", canonical_json_bytes(metadata))
            with self.path.open("r+b") as stream:
                stream.truncate(error.valid_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            return self.append(
                JournalRecordType.RECOVERY_NOTE,
                {"code": "truncated_tail_preserved", **metadata},
            )
        raise JournalCorruptionError("journal does not contain a truncated tail")
