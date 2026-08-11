"""Durable project-level active writer ownership."""

from __future__ import annotations

from dataclasses import dataclass

from .codec import parse_timestamp
from .errors import SerializationError, UnsupportedSchemaError


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
        if not self.project_id.startswith("prj_") or not self.run_id.startswith("run_"):
            raise SerializationError("invalid active-run identity")
        parse_timestamp(self.created_at)
