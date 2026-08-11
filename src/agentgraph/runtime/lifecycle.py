"""Durable project-level active writer ownership."""

from __future__ import annotations

from dataclasses import dataclass

from .codec import parse_timestamp
from .errors import UnsupportedSchemaError
from .ids import validate_project_id, validate_run_id


@dataclass(frozen=True, slots=True)
class ActiveRunRecord:
    """Diagnostic durable ownership of one unfinished project writer run."""

    project_id: str
    run_id: str
    created_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise UnsupportedSchemaError("unsupported active-run schema")
        validate_project_id(self.project_id)
        validate_run_id(self.run_id)
        parse_timestamp(self.created_at)
