"""Independent reconciliation and component-aware path capabilities."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentgraph.work import RepoPathSpec, WorkItem, WorkPackage, WorkScope, WorkSourceSnapshot

from .errors import ChangePathError, WorkCapabilityMismatchError

if TYPE_CHECKING:
    from agentgraph.integration import SelectionPlan


def reconcile_write_capability(
    snapshot: WorkSourceSnapshot,
    selection: SelectionPlan,
    package: WorkPackage,
) -> tuple[RepoPathSpec, ...]:
    item = _one(snapshot.items, "item_id", selection.item_id)
    scope = _one(snapshot.scopes, "scope_id", selection.scope_id)
    if not isinstance(item, WorkItem) or not isinstance(scope, WorkScope):
        raise WorkCapabilityMismatchError("selected source objects are invalid")
    comparisons = (
        package.item_id == selection.item_id == item.item_id,
        package.scope_id == selection.scope_id == item.scope_id == scope.scope_id,
        package.parent_scope_id == scope.parent_scope_id,
        package.source_revision.fingerprint == snapshot.revision.fingerprint,
        package.implementation_paths == item.implementation_paths,
        package.test_paths == item.test_paths,
        package.branch_hint == scope.branch_hint,
        package.base_branch_hint == scope.base_branch_hint,
    )
    expected = stable_path_union(item.implementation_paths, item.test_paths)
    if not all(comparisons) or package.allowed_paths != expected:
        raise WorkCapabilityMismatchError("work_package_capability_mismatch")
    for spec in expected:
        normalize_repo_path(spec.path)
    return expected


def stable_path_union(*groups: tuple[RepoPathSpec, ...]) -> tuple[RepoPathSpec, ...]:
    values: list[RepoPathSpec] = []
    seen: set[tuple[str, bool]] = set()
    for group in groups:
        for spec in group:
            key = (spec.path, spec.directory_hint)
            if key not in seen:
                seen.add(key)
                values.append(spec)
    return tuple(values)


def normalize_repo_path(path: str) -> PurePosixPath:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ChangePathError("path must be repository-relative POSIX")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or path != pure.as_posix():
        raise ChangePathError("path must be normalized repository-relative POSIX")
    if len(pure.parts) == 0 or any(":" in part for part in pure.parts) or path.startswith("//"):
        raise ChangePathError("absolute, drive, and UNC paths are forbidden")
    return pure


def path_is_allowed(path: str, allowed: tuple[RepoPathSpec, ...]) -> bool:
    candidate = normalize_repo_path(path)
    for spec in allowed:
        authority = normalize_repo_path(spec.path)
        if spec.directory_hint:
            if (
                len(candidate.parts) > len(authority.parts)
                and candidate.parts[: len(authority.parts)] == authority.parts
            ):
                return True
        elif candidate == authority:
            return True
    return False


def capability_fingerprint(paths: tuple[RepoPathSpec, ...]) -> str:
    payload = [{"path": item.path, "directory_hint": item.directory_hint} for item in paths]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _one(values: tuple[object, ...], field: str, expected: str | None) -> object:
    matches = [value for value in values if getattr(value, field) == expected]
    if len(matches) != 1:
        raise WorkCapabilityMismatchError("selected source object is absent or ambiguous")
    return matches[0]
