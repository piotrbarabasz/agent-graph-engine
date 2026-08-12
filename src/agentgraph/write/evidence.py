"""Canonical immutable operation evidence outside the target repository."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.runtime.codec import canonical_json_bytes, encode_value, parse_json_bytes

from .errors import WorkspaceError


def write_evidence(path: Path, *, context: dict[str, Any], payload: Any) -> str:
    if path.exists() or path.is_symlink():
        raise WorkspaceError("operation evidence is immutable")
    body = {
        "schema_version": 1,
        **context,
        "payload": encode_value(payload),
    }
    digest = f"sha256:{hashlib.sha256(canonical_json_bytes(body)).hexdigest()}"
    document = {**body, "content_digest": digest}
    atomic_write_bytes(path, canonical_json_bytes(document))
    return path.name


def read_evidence(path: Path) -> dict[str, Any]:
    """Read one canonical evidence envelope and verify its content digest."""

    try:
        document = parse_json_bytes(path.read_bytes())
    except OSError as exc:
        raise WorkspaceError("required operation evidence is unavailable") from exc
    if not isinstance(document, dict) or "content_digest" not in document:
        raise WorkspaceError("operation evidence envelope is invalid")
    digest = document.pop("content_digest")
    expected = f"sha256:{hashlib.sha256(canonical_json_bytes(document)).hexdigest()}"
    if digest != expected or document.get("schema_version") != 1:
        raise WorkspaceError("operation evidence digest is invalid")
    return document
