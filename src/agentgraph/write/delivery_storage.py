"""Fail-closed storage validation for run-scoped delivery review evidence."""

from __future__ import annotations

import stat
from pathlib import Path

from .errors import DeliveryReviewStorageError


def verify_delivery_storage(run_path: Path, *relevant_paths: Path) -> Path:
    root = run_path / "delivery-review"
    try:
        canonical_run = run_path.resolve(strict=True)
    except OSError as exc:
        raise DeliveryReviewStorageError("delivery_review_storage_invalid") from exc
    for candidate in (root, root / "provider", *relevant_paths):
        try:
            relative = candidate.relative_to(run_path)
        except ValueError as exc:
            raise DeliveryReviewStorageError("delivery_review_storage_invalid") from exc
        current = run_path
        for index, part in enumerate(relative.parts):
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise DeliveryReviewStorageError("delivery_review_storage_invalid") from exc
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse):
                raise DeliveryReviewStorageError("delivery_review_storage_invalid")
            is_final = index == len(relative.parts) - 1
            expects_file = is_final and candidate.name.endswith(".json")
            if expects_file:
                if not stat.S_ISREG(metadata.st_mode):
                    raise DeliveryReviewStorageError("delivery_review_storage_invalid")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise DeliveryReviewStorageError("delivery_review_storage_invalid")
            try:
                current.resolve(strict=True).relative_to(canonical_run)
            except (OSError, ValueError) as exc:
                raise DeliveryReviewStorageError("delivery_review_storage_invalid") from exc
    return root
