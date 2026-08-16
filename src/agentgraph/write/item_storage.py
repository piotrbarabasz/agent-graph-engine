"""Fail-closed validation for per-item durable evidence storage."""

from __future__ import annotations

import stat
from pathlib import Path

from .errors import ItemEvidenceError


def verify_item_storage(
    run_path: Path,
    item_root: Path,
    evidence_root: Path,
    *relevant_paths: Path,
) -> None:
    """Reject links, reparse points, and paths escaping one durable run."""

    try:
        canonical_run = run_path.resolve(strict=True)
    except OSError as exc:
        raise ItemEvidenceError("item_evidence_invalid") from exc
    items = run_path / "items"
    if item_root.parent != items or evidence_root not in {run_path, item_root}:
        raise ItemEvidenceError("item_evidence_invalid")
    candidates = (
        items,
        item_root,
        item_root / "write-inputs.json",
        evidence_root / "operations",
        evidence_root / "provider",
        *relevant_paths,
    )
    for candidate in candidates:
        _verify_candidate(run_path, canonical_run, candidate)


def _verify_candidate(run_path: Path, canonical_run: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(run_path)
    except ValueError as exc:
        raise ItemEvidenceError("item_evidence_invalid") from exc
    current = run_path
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ItemEvidenceError("item_evidence_invalid") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse):
            raise ItemEvidenceError("item_evidence_invalid")
        try:
            current.resolve(strict=True).relative_to(canonical_run)
        except (OSError, ValueError) as exc:
            raise ItemEvidenceError("item_evidence_invalid") from exc
