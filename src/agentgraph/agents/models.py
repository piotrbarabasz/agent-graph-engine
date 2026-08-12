"""Immutable provider-neutral read-only agent contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from agentgraph.runtime.codec import encode_value, sha256_digest

from .errors import AgentContextError, AgentResponseContractError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z", re.ASCII)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)


def _immutable_json(value: Any) -> Any:
    encode_value(value)
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_immutable_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class AgentRequest:
    operation_id: str
    prompt: str
    output_schema: Mapping[str, Any]
    output_schema_id: str
    input_digest: str

    def __post_init__(self) -> None:
        if _ID.fullmatch(self.operation_id) is None or _ID.fullmatch(self.output_schema_id) is None:
            raise AgentResponseContractError("agent request identifiers are invalid")
        if not self.prompt or "\x00" in self.prompt:
            raise AgentResponseContractError("agent prompt must be non-empty NUL-free text")
        object.__setattr__(self, "output_schema", _immutable_json(self.output_schema))
        if self.input_digest != self.compute_digest(
            self.operation_id, self.prompt, self.output_schema, self.output_schema_id
        ):
            raise AgentResponseContractError("agent input digest does not match canonical request")

    @classmethod
    def create(
        cls,
        operation_id: str,
        prompt: str,
        output_schema: Mapping[str, Any],
        output_schema_id: str,
    ) -> AgentRequest:
        digest = cls.compute_digest(operation_id, prompt, output_schema, output_schema_id)
        return cls(operation_id, prompt, output_schema, output_schema_id, digest)

    @staticmethod
    def compute_digest(
        operation_id: str,
        prompt: str,
        output_schema: Mapping[str, Any],
        output_schema_id: str,
    ) -> str:
        return sha256_digest(
            {
                "operation_id": operation_id,
                "prompt": prompt,
                "output_schema": output_schema,
                "output_schema_id": output_schema_id,
            }
        )


@dataclass(frozen=True, slots=True)
class AgentContext:
    project_id: str
    run_id: str
    node_id: str
    node_attempt_id: str
    repository_root: Path
    runtime_directory: Path
    baseline_head: str
    source_revision: str
    provider_invocation_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value and "\x00" not in value
            for value in (self.project_id, self.run_id, self.node_id, self.node_attempt_id)
        ):
            raise AgentContextError("agent context identity is invalid")
        if self.provider_invocation_id is not None and (
            not isinstance(self.provider_invocation_id, str)
            or not self.provider_invocation_id
            or "\x00" in self.provider_invocation_id
        ):
            raise AgentContextError("agent provider invocation identity is invalid")
        if _DIGEST.fullmatch(self.source_revision) is None:
            raise AgentContextError("agent source revision must be a SHA-256 fingerprint")
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.baseline_head):
            raise AgentContextError("agent baseline must be a lowercase Git object ID")
        root = self.repository_root.resolve(strict=True)
        runtime = self.runtime_directory.resolve(strict=False)
        if (
            not root.is_dir()
            or root.is_symlink()
            or not runtime.parent.is_dir()
            or runtime.exists()
            or runtime.is_symlink()
        ):
            raise AgentContextError("agent repository must exist and attempt directory must be new")
        try:
            runtime.relative_to(root)
        except ValueError:
            pass
        else:
            raise AgentContextError("agent evidence must remain outside the repository")
        object.__setattr__(self, "repository_root", root)
        object.__setattr__(self, "runtime_directory", runtime)


@dataclass(frozen=True, slots=True)
class AgentResponse:
    payload: Mapping[str, Any]
    provider_name: str
    provider_version: str
    model: str | None
    input_digest: str
    output_digest: str
    evidence_reference: str
    command_receipt: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _immutable_json(self.payload))
        if not self.provider_name or not self.provider_version:
            raise AgentResponseContractError("agent provider identity is required")
        if (
            _DIGEST.fullmatch(self.input_digest) is None
            or _DIGEST.fullmatch(self.output_digest) is None
        ):
            raise AgentResponseContractError("agent response digests are invalid")
        if not self.evidence_reference or "\x00" in self.evidence_reference:
            raise AgentResponseContractError("agent evidence reference is invalid")
        evidence = PurePosixPath(self.evidence_reference)
        if (
            "\\" in self.evidence_reference
            or evidence.is_absolute()
            or ".." in evidence.parts
            or "." in evidence.parts
            or evidence.as_posix() != self.evidence_reference
        ):
            raise AgentResponseContractError(
                "agent evidence reference must be safe runtime-relative POSIX"
            )
        if self.output_digest != sha256_digest(self.payload):
            raise AgentResponseContractError(
                "agent response digest does not match the structured payload"
            )
        if self.command_receipt is not None:
            object.__setattr__(self, "command_receipt", _immutable_json(self.command_receipt))
