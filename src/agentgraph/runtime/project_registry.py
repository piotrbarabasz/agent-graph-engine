"""Locked, atomic registry of canonical local project roots."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .atomic import atomic_write_bytes
from .codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
    format_timestamp,
    parse_json_bytes,
    parse_timestamp,
    utc_now,
)
from .errors import ProjectRegistryError, SerializationError, UnsupportedSchemaError
from .ids import generate_project_id
from .locking import AdvisoryFileLock
from .paths import RuntimePaths


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """Stable identity for one canonical local working copy."""

    project_id: str
    canonical_root: str
    created_at: str
    updated_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not self.project_id.startswith("prj_"):
            raise ProjectRegistryError("invalid project record")
        parse_timestamp(self.created_at)
        parse_timestamp(self.updated_at)


def canonicalize_root(root: Path | str) -> str:
    """Return an absolute, resolved, OS-normalized working-copy identity."""

    return os.path.normcase(str(Path(root).expanduser().resolve()))


class ProjectRegistry:
    """Manage project identities under a separate registry OS lock."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        project_id_factory: Callable[[], str] = generate_project_id,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.paths = paths
        self.project_id_factory = project_id_factory
        self.now = now

    def register(self, canonical_root: Path | str) -> ProjectRecord:
        root = canonicalize_root(canonical_root)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        with AdvisoryFileLock(self.paths.registry_lock, blocking=True):
            records = self._load_unlocked()
            for record in records:
                if record.canonical_root == root:
                    self._validate_project_file(record)
                    return record
            timestamp = format_timestamp(self.now())
            record = ProjectRecord(self.project_id_factory(), root, timestamp, timestamp)
            if any(item.project_id == record.project_id for item in records):
                raise ProjectRegistryError("project ID generator produced a duplicate")
            project_dir = self.paths.project(record.project_id)
            project_dir.mkdir(parents=True, exist_ok=False)
            (project_dir / "runs").mkdir()
            atomic_write_bytes(
                self.paths.project_record(record.project_id), canonical_json_bytes(record)
            )
            self._write_unlocked((*records, record))
            return record

    def get(self, project_id: str) -> ProjectRecord:
        records = self._load_unlocked()
        try:
            record = next(item for item in records if item.project_id == project_id)
        except StopIteration as exc:
            raise ProjectRegistryError(f"unknown project ID: {project_id}") from exc
        self._validate_project_file(record)
        return record

    def find_by_root(self, canonical_root: Path | str) -> ProjectRecord | None:
        root = canonicalize_root(canonical_root)
        for record in self._load_unlocked():
            if record.canonical_root == root:
                self._validate_project_file(record)
                return record
        return None

    def _load_unlocked(self) -> tuple[ProjectRecord, ...]:
        if not self.paths.registry.exists():
            return ()
        try:
            envelope = parse_json_bytes(self.paths.registry.read_bytes())
            if set(envelope) != {"schema_version", "projects"}:
                raise ProjectRegistryError("invalid registry envelope fields")
            if envelope["schema_version"] != 1:
                raise UnsupportedSchemaError("unsupported registry schema")
            if not isinstance(envelope["projects"], list):
                raise ProjectRegistryError("registry projects must be an array")
            records = tuple(decode_value(item, ProjectRecord) for item in envelope["projects"])
            if len({item.project_id for item in records}) != len(records):
                raise ProjectRegistryError("duplicate project ID in registry")
            if len({item.canonical_root for item in records}) != len(records):
                raise ProjectRegistryError("duplicate canonical root in registry")
            return records
        except (OSError, SerializationError, TypeError) as exc:
            if isinstance(exc, ProjectRegistryError):
                raise
            raise ProjectRegistryError("corrupted project registry") from exc

    def _write_unlocked(self, records: tuple[ProjectRecord, ...]) -> None:
        envelope = {"schema_version": 1, "projects": [encode_value(item) for item in records]}
        atomic_write_bytes(self.paths.registry, canonical_json_bytes(envelope))

    def _validate_project_file(self, expected: ProjectRecord) -> None:
        path = self.paths.project_record(expected.project_id)
        try:
            actual = decode_value(parse_json_bytes(path.read_bytes()), ProjectRecord)
        except (OSError, SerializationError) as exc:
            raise ProjectRegistryError("project.json is missing or corrupt") from exc
        if actual != expected:
            raise ProjectRegistryError("project.json disagrees with registry")
