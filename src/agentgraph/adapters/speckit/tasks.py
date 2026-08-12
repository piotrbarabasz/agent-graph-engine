"""Strict Markdown work-item block parsing for the compatibility adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentgraph.work import (
    IssueSeverity,
    SourceLocation,
    ValidationOrigin,
    WorkItem,
    WorkItemStatus,
    WorkRisk,
    WorkSourceFormatError,
    WorkSourceIssue,
    WorkSourcePathError,
    parse_validation_checks,
)

from .paths import parse_repo_path_list
from .schema import CHILD_ID, ITEM_ID, PARENT_ID

_HEADER = re.compile(r"^\s*-\s+\[([ xX])\]\s+(T\d{3}[A-Z]?)\s+(.+?)\s*$")
_REQUIRED_FIELDS = (
    "milestone",
    "epic",
    "risk",
    "implementation files",
    "test files",
    "validation commands",
    "final pr review required",
    "goal",
    "dependencies",
    "acceptance criteria",
    "test requirements",
    "parallelizable",
    "notes",
)
_FIELD = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?P<name>"
    + "|".join(re.escape(field) for field in _REQUIRED_FIELDS)
    + r"):(?:\*\*)?\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_NO_VALUES = {"none", "n/a", "na", "[]"}


@dataclass(frozen=True, slots=True)
class ParsedTask:
    item: WorkItem
    declared_parent_id: str


def parse_tasks_document(
    raw: bytes,
    relative_path: str,
    repository_root: Path,
    issues: list[WorkSourceIssue],
) -> tuple[ParsedTask, ...]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _issue(issues, "tasks_not_utf8", "tasks document must be UTF-8", relative_path)
        return ()
    lines = text.splitlines()
    headers = [index for index, line in enumerate(lines) if _HEADER.match(line)]
    parsed: list[ParsedTask] = []
    seen: set[str] = set()
    for position, start in enumerate(headers):
        end = headers[position + 1] if position + 1 < len(headers) else len(lines)
        match = _HEADER.match(lines[start])
        assert match is not None
        checked, item_id, title = match.groups()
        location = SourceLocation(relative_path, start + 1)
        if item_id in seen:
            _issue(
                issues,
                "duplicate_item_id",
                "duplicate item id in tasks document",
                relative_path,
                start + 1,
                item_id=item_id,
            )
            continue
        seen.add(item_id)
        fields, field_lines = _parse_fields(
            lines[start + 1 : end], start + 2, relative_path, item_id, issues
        )
        missing = [field for field in _REQUIRED_FIELDS if field not in fields]
        for field in missing:
            _issue(
                issues,
                "missing_task_field",
                f"item requires field {field}",
                relative_path,
                start + 1,
                item_id=item_id,
            )
        if missing:
            continue
        task = _build_task(
            item_id,
            title,
            checked,
            fields,
            field_lines,
            location,
            repository_root,
            issues,
        )
        if task is not None:
            parsed.append(task)
    return tuple(parsed)


def _parse_fields(
    lines: list[str],
    first_line: int,
    relative_path: str,
    item_id: str,
    issues: list[WorkSourceIssue],
) -> tuple[dict[str, str], dict[str, int]]:
    fields: dict[str, str] = {}
    field_lines: dict[str, int] = {}
    current: str | None = None
    for offset, line in enumerate(lines):
        match = _FIELD.match(line)
        if match:
            name = match.group("name").casefold()
            if name in fields:
                _issue(
                    issues,
                    "duplicate_task_field",
                    f"duplicate item field {name}",
                    relative_path,
                    first_line + offset,
                    item_id=item_id,
                )
                current = None
                continue
            fields[name] = match.group("value").strip()
            field_lines[name] = first_line + offset
            current = name
        elif current is not None and line.strip() and not line.lstrip().startswith("#"):
            fields[current] = f"{fields[current]}\n{line.strip()}".strip()
    return fields, field_lines


def _build_task(
    item_id: str,
    title: str,
    checked: str,
    fields: dict[str, str],
    field_lines: dict[str, int],
    location: SourceLocation,
    root: Path,
    issues: list[WorkSourceIssue],
) -> ParsedTask | None:
    parent_id = _plain(fields["milestone"])
    scope_id = _plain(fields["epic"])
    valid = True
    for field in ("goal", "acceptance criteria", "test requirements", "notes"):
        if not fields[field].strip():
            _task_issue(
                issues,
                "empty_task_field",
                f"item field {field} must not be empty",
                location,
                item_id,
            )
            valid = False
    if PARENT_ID.fullmatch(parent_id) is None:
        _task_issue(issues, "invalid_task_parent", "invalid parent scope id", location, item_id)
        valid = False
    if CHILD_ID.fullmatch(scope_id) is None:
        _task_issue(issues, "invalid_task_scope", "invalid owner scope id", location, item_id)
        valid = False
    try:
        risk = WorkRisk(_plain(fields["risk"]).casefold())
    except ValueError:
        _task_issue(issues, "invalid_task_risk", "unknown item risk", location, item_id)
        valid = False
        risk = WorkRisk.LOW
    try:
        implementation = parse_repo_path_list(root, fields["implementation files"])
        tests = parse_repo_path_list(root, fields["test files"])
    except WorkSourcePathError as exc:
        _task_issue(issues, "unsafe_declared_path", str(exc), location, item_id)
        valid = False
        implementation = ()
        tests = ()
    dependencies = _dependencies(fields["dependencies"], location, item_id, issues)
    if dependencies is None:
        valid = False
        dependencies = ()
    command_location = SourceLocation(location.path, field_lines["validation commands"])
    try:
        checks = parse_validation_checks(
            fields["validation commands"],
            origin=ValidationOrigin.ITEM,
            source_location=command_location,
        )
    except WorkSourceFormatError as exc:
        _task_issue(issues, "invalid_validation_command", str(exc), location, item_id)
        valid = False
        checks = ()
    review = _yes_no(fields["final pr review required"])
    parallel = _yes_no(fields["parallelizable"])
    if review is None:
        _task_issue(issues, "invalid_final_review", "expected yes or no", location, item_id)
        valid = False
    if parallel is None:
        _task_issue(issues, "invalid_parallelizable", "expected yes or no", location, item_id)
        valid = False
    if not valid:
        return None
    item = WorkItem(
        item_id=item_id,
        title=title.strip(),
        scope_id=scope_id,
        parent_scope_id=parent_id,
        status=(WorkItemStatus.COMPLETED if checked.casefold() == "x" else WorkItemStatus.PENDING),
        risk=risk,
        goal=fields["goal"],
        acceptance_criteria=(fields["acceptance criteria"],),
        test_requirements=(fields["test requirements"],),
        dependencies=dependencies,
        implementation_paths=implementation,
        test_paths=tests,
        validation_checks=checks,
        final_review_required=bool(review),
        parallelizable=bool(parallel),
        notes=(fields["notes"],),
        source_location=location,
    )
    return ParsedTask(item, parent_id)


def _dependencies(
    raw: str,
    location: SourceLocation,
    item_id: str,
    issues: list[WorkSourceIssue],
) -> tuple[str, ...] | None:
    if raw.strip().casefold() in _NO_VALUES:
        return ()
    values = tuple(_plain(part.strip()) for part in raw.split(","))
    if any(ITEM_ID.fullmatch(value) is None for value in values):
        _task_issue(
            issues, "invalid_item_dependency", "invalid item dependency id", location, item_id
        )
        return None
    if len(set(values)) != len(values):
        _task_issue(
            issues, "duplicate_item_dependency", "duplicate item dependency", location, item_id
        )
        return None
    if item_id in values:
        _task_issue(
            issues, "self_item_dependency", "item cannot depend on itself", location, item_id
        )
        return None
    return values


def _plain(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped.startswith("`") and stripped.endswith("`"):
        return stripped[1:-1].strip()
    return stripped


def _yes_no(value: str) -> bool | None:
    normalized = _plain(value).casefold()
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    return None


def _task_issue(
    issues: list[WorkSourceIssue],
    code: str,
    message: str,
    location: SourceLocation,
    item_id: str,
) -> None:
    issues.append(
        WorkSourceIssue(
            code,
            IssueSeverity.ERROR,
            message,
            location,
            item_id=item_id,
        )
    )


def _issue(
    issues: list[WorkSourceIssue],
    code: str,
    message: str,
    path: str,
    line: int | None = None,
    item_id: str | None = None,
) -> None:
    issues.append(
        WorkSourceIssue(
            code,
            IssueSeverity.ERROR,
            message,
            SourceLocation(path, line),
            item_id=item_id,
        )
    )
