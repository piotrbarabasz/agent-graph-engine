"""Fail-closed storage for immutable publication evidence."""

from __future__ import annotations

import stat
from pathlib import Path

from agentgraph.runtime.codec import decode_value

from .errors import PublishEvidenceError
from .evidence import read_evidence, write_evidence


def verify_publish_storage(run_path: Path, *paths: Path) -> Path:
    root = run_path / "publish"
    try:
        canonical_run = run_path.resolve(strict=True)
    except OSError as exc:
        raise PublishEvidenceError("publish_storage_invalid") from exc
    for candidate in (root, *paths):
        try:
            relative = candidate.relative_to(run_path)
        except ValueError as exc:
            raise PublishEvidenceError("publish_storage_invalid") from exc
        current = run_path
        for index, part in enumerate(relative.parts):
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PublishEvidenceError("publish_storage_invalid") from exc
            attributes = getattr(metadata, "st_file_attributes", 0)
            if stat.S_ISLNK(metadata.st_mode) or bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise PublishEvidenceError("publish_storage_invalid")
            final = index == len(relative.parts) - 1
            if final and candidate.suffix == ".json":
                valid = stat.S_ISREG(metadata.st_mode)
            else:
                valid = stat.S_ISDIR(metadata.st_mode)
            if not valid:
                raise PublishEvidenceError("publish_storage_invalid")
            try:
                current.resolve(strict=True).relative_to(canonical_run)
            except (OSError, ValueError) as exc:
                raise PublishEvidenceError("publish_storage_invalid") from exc
    return root


def persist_once(run_path: Path, name: str, value: object, value_type: type, context: dict):
    root = verify_publish_storage(run_path)
    if not root.exists():
        root.mkdir()
    verify_publish_storage(run_path, root)
    path = root / name
    verify_publish_storage(run_path, path)
    if path.exists():
        try:
            document = read_evidence(path)
            restored = decode_value(document.get("payload"), value_type)
        except Exception as exc:
            raise PublishEvidenceError("publish_evidence_invalid") from exc
        if restored != value or any(document.get(key) != item for key, item in context.items()):
            raise PublishEvidenceError("publish_evidence_mismatch")
        return restored
    write_evidence(path, context=context, payload=value)
    return value


def load_typed(run_path: Path, name: str, value_type: type, context: dict):
    path = verify_publish_storage(run_path, run_path / "publish" / name) / name
    if not path.exists():
        return None
    try:
        document = read_evidence(path)
        value = decode_value(document.get("payload"), value_type)
    except Exception as exc:
        raise PublishEvidenceError("publish_evidence_invalid") from exc
    if any(document.get(key) != item for key, item in context.items()):
        raise PublishEvidenceError("publish_evidence_mismatch")
    return value
