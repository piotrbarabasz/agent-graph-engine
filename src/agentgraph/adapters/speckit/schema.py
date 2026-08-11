"""Typed compatibility schema for workstream manifests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agentgraph.work import (
    IssueSeverity,
    SourceLocation,
    SourcePolicyHints,
    WorkRisk,
    WorkScopeStatus,
    WorkSourceIssue,
)

PARENT_ID = re.compile(r"M\d{3}\Z")
CHILD_ID = re.compile(r"E\d{3}\Z")
ITEM_ID = re.compile(r"T\d{3}[A-Z]?\Z")


@dataclass(frozen=True, slots=True)
class ParentManifest:
    scope_id: str
    title: str
    status: WorkScopeStatus
    goal: str
    child_ids: tuple[str, ...]
    completion_criteria: tuple[str, ...]
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class ChildManifest:
    scope_id: str
    title: str
    parent_id: str
    feature: str
    base_branch: str
    branch: str
    status: WorkScopeStatus
    risk: WorkRisk
    dependencies: tuple[str, ...]
    item_ids: tuple[str, ...]
    required_check_declarations: tuple[str, ...]
    policy_hints: SourcePolicyHints
    location: SourceLocation


def parse_manifest(
    data: Any,
    location: SourceLocation,
    issues: list[WorkSourceIssue],
) -> ParentManifest | ChildManifest | None:
    if not isinstance(data, dict):
        _issue(issues, "manifest_not_mapping", "manifest root must be a mapping", location)
        return None
    source_id = data.get("id")
    if not isinstance(source_id, str):
        _issue(issues, "missing_manifest_id", "manifest requires a string id", location)
        return None
    if PARENT_ID.fullmatch(source_id):
        return _parse_parent(data, source_id, location, issues)
    if CHILD_ID.fullmatch(source_id):
        return _parse_child(data, source_id, location, issues)
    _issue(issues, "invalid_manifest_id", "manifest id has an unsupported format", location)
    return None


def _parse_parent(
    data: dict[str, Any],
    scope_id: str,
    location: SourceLocation,
    issues: list[WorkSourceIssue],
) -> ParentManifest | None:
    title = _string(data, "title", location, issues, scope_id)
    status = _status(data, location, issues, scope_id)
    goal = _string(data, "goal", location, issues, scope_id)
    children = _string_list(data, "epics", location, issues, scope_id)
    criteria = _string_list(data, "completion_criteria", location, issues, scope_id)
    if None in {title, status, goal} or children is None or criteria is None:
        return None
    _duplicates(children, "duplicate_child_scope", location, issues, scope_id)
    return ParentManifest(scope_id, title, status, goal, children, criteria, location)


def _parse_child(
    data: dict[str, Any],
    scope_id: str,
    location: SourceLocation,
    issues: list[WorkSourceIssue],
) -> ChildManifest | None:
    title = _string(data, "title", location, issues, scope_id)
    parent = _string(data, "milestone", location, issues, scope_id)
    feature = _string(data, "feature", location, issues, scope_id)
    base = _string(data, "base_branch", location, issues, scope_id)
    branch = _string(data, "branch", location, issues, scope_id)
    status = _status(data, location, issues, scope_id)
    risk = _risk(data, location, issues, scope_id)
    dependencies = _string_list(data, "depends_on", location, issues, scope_id)
    item_ids = _string_list(data, "tasks", location, issues, scope_id)
    checks = _string_list(data, "required_checks", location, issues, scope_id)
    policy = _policy(data, location, issues, scope_id)
    if (
        None in {title, parent, feature, base, branch, status, risk}
        or dependencies is None
        or item_ids is None
        or checks is None
        or policy is None
    ):
        return None
    if base == branch:
        _issue(
            issues,
            "branch_equals_base_branch",
            "branch and base_branch must differ",
            location,
            scope_id,
        )
    _duplicates(dependencies, "duplicate_scope_dependency", location, issues, scope_id)
    _duplicates(item_ids, "duplicate_manifest_item", location, issues, scope_id)
    if scope_id in dependencies:
        _issue(
            issues,
            "self_scope_dependency",
            "scope cannot depend on itself",
            location,
            scope_id,
        )
    if status is WorkScopeStatus.ACTIVE and not item_ids:
        _issue(
            issues,
            "active_scope_empty",
            "active scope must declare at least one item",
            location,
            scope_id,
        )
    return ChildManifest(
        scope_id,
        title,
        parent,
        feature,
        base,
        branch,
        status,
        risk,
        dependencies,
        item_ids,
        checks,
        policy,
        location,
    )


def _string(
    data: dict[str, Any],
    field: str,
    location: SourceLocation,
    issues: list[WorkSourceIssue],
    scope_id: str,
) -> str | None:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        _issue(
            issues,
            "invalid_manifest_field",
            f"manifest field {field} must be a non-empty string",
            location,
            scope_id,
        )
        return None
    return value.strip()


def _string_list(
    data: dict[str, Any],
    field: str,
    location: SourceLocation,
    issues: list[WorkSourceIssue],
    scope_id: str,
) -> tuple[str, ...] | None:
    value = data.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _issue(
            issues,
            "invalid_manifest_field",
            f"manifest field {field} must be a list of strings",
            location,
            scope_id,
        )
        return None
    return tuple(item.strip() for item in value)


def _status(
    data: dict[str, Any],
    location: SourceLocation,
    issues: list[WorkSourceIssue],
    scope_id: str,
) -> WorkScopeStatus | None:
    raw = data.get("status")
    try:
        return WorkScopeStatus(raw)
    except (TypeError, ValueError):
        _issue(issues, "invalid_scope_status", "unknown scope status", location, scope_id)
        return None


def _risk(
    data: dict[str, Any],
    location: SourceLocation,
    issues: list[WorkSourceIssue],
    scope_id: str,
) -> WorkRisk | None:
    raw = data.get("risk")
    try:
        return WorkRisk(raw)
    except (TypeError, ValueError):
        _issue(issues, "invalid_scope_risk", "unknown scope risk", location, scope_id)
        return None


def _policy(
    data: dict[str, Any],
    location: SourceLocation,
    issues: list[WorkSourceIssue],
    scope_id: str,
) -> SourcePolicyHints | None:
    pr = data.get("pr_policy")
    commit = data.get("commit_policy")
    expected_pr = {
        "one_pr_per_epic": True,
        "merge_requires_human": True,
        "auto_merge": False,
    }
    expected_commit = {
        "one_commit_per_task": True,
        "commit_requires_human": True,
        "auto_commit": False,
    }
    if not isinstance(pr, dict) or not isinstance(commit, dict):
        _issue(
            issues,
            "invalid_source_policy",
            "source policies must be mappings",
            location,
            scope_id,
        )
        return None
    if any(pr.get(key) is not value for key, value in expected_pr.items()) or any(
        commit.get(key) is not value for key, value in expected_commit.items()
    ):
        _issue(
            issues,
            "unsafe_source_policy",
            "source policy is incompatible with the read-only adapter contract",
            location,
            scope_id,
        )
        return None
    return SourcePolicyHints(True, True, False, True, True, False)


def _duplicates(
    values: tuple[str, ...],
    code: str,
    location: SourceLocation,
    issues: list[WorkSourceIssue],
    scope_id: str,
) -> None:
    if len(set(values)) != len(values):
        _issue(issues, code, "manifest list contains duplicate identifiers", location, scope_id)


def _issue(
    issues: list[WorkSourceIssue],
    code: str,
    message: str,
    location: SourceLocation,
    scope_id: str | None = None,
) -> None:
    issues.append(WorkSourceIssue(code, IssueSeverity.ERROR, message, location, scope_id=scope_id))
