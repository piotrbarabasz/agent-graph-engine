"""Idempotent terminal run receipt contract."""

from __future__ import annotations

from dataclasses import dataclass

from .codec import parse_timestamp
from .errors import SerializationError, UnsupportedSchemaError
from .ids import validate_project_id, validate_run_id


@dataclass(frozen=True, slots=True)
class FinalReceipt:
    """Durable summary of one terminal state."""

    project_id: str
    run_id: str
    final_status: str
    final_state_version: int
    final_state_digest: str
    last_journal_seq: int
    finished_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise UnsupportedSchemaError("unsupported final receipt schema")
        validate_project_id(self.project_id)
        validate_run_id(self.run_id)
        if self.final_state_version < 0 or self.last_journal_seq < 1:
            raise SerializationError("invalid final receipt versions")
        parse_timestamp(self.finished_at)
