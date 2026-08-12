"""Strict model-output DTOs and JSON Schema."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from agentgraph.runtime.codec import canonical_json_bytes, encode_value


class CodexProposalStatus(StrEnum):
    CHANGES = "changes"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CodexFileProposal:
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class CodexProposal:
    schema_version: int
    status: CodexProposalStatus
    changes: tuple[CodexFileProposal, ...]
    reason_code: str | None
    message: str | None

    @property
    def digest(self) -> str:
        raw = canonical_json_bytes(encode_value(self))
        return f"sha256:{hashlib.sha256(raw).hexdigest()}"


CODEX_PROPOSAL_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "status", "changes", "reason_code", "message"],
    "properties": {
        "schema_version": {"const": 1},
        "status": {"enum": ["changes", "blocked"]},
        "changes": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
            },
        },
        "reason_code": {"type": ["string", "null"]},
        "message": {"type": ["string", "null"], "maxLength": 2000},
    },
}
