"""Canonical immutable operation evidence outside the target repository."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.runtime.codec import canonical_json_bytes, encode_value

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
