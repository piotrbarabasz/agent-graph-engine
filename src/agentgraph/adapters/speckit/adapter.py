"""Read-only snapshot-first adapter for the supported source layout."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from agentgraph.work import (
    InvalidWorkSourceError,
    IssueSeverity,
    ScopeSelection,
    SelectionKind,
    SourceDocumentRevision,
    SourceLocation,
    ValidationOrigin,
    WorkItem,
    WorkItemNotFoundError,
    WorkItemStatus,
    WorkPackage,
    WorkScope,
    WorkScopeNotFoundError,
    WorkScopeStatus,
    WorkSelection,
    WorkSourceFormatError,
    WorkSourceIssue,
    WorkSourcePathError,
    WorkSourceRevision,
    WorkSourceSnapshot,
    WorkSourceValidation,
    parse_validation_checks,
)

from .paths import SpecKitLayout, resolve_repository_path
from .schema import CHILD_ID, ChildManifest, ParentManifest, parse_manifest
from .tasks import ParsedTask, parse_tasks_document
from .yaml_loader import load_yaml

_MAX_SOURCE_BYTES = 4 * 1024 * 1024


class SpecKitAdapter:
    """Translate one filesystem source snapshot into neutral immutable work contracts."""

    def __init__(self, layout: SpecKitLayout) -> None:
        self.layout = layout

    def validate(self) -> WorkSourceValidation:
        validation, _ = self._collect()
        return validation

    def snapshot(self) -> WorkSourceSnapshot:
        validation, snapshot = self._collect()
        if not validation.ok or snapshot is None:
            raise InvalidWorkSourceError(validation)
        return snapshot

    def get_scope(self, snapshot: WorkSourceSnapshot, scope_id: str) -> WorkScope:
        for scope in snapshot.scopes:
            if scope.scope_id == scope_id:
                return scope
        raise WorkScopeNotFoundError(f"unknown scope: {scope_id}")

    def get_item(self, snapshot: WorkSourceSnapshot, item_id: str) -> WorkItem:
        for item in snapshot.items:
            if item.item_id == item_id:
                return item
        raise WorkItemNotFoundError(f"unknown item: {item_id}")

    def next_ready_item(self, snapshot: WorkSourceSnapshot, scope_id: str) -> WorkSelection:
        scope = self.get_scope(snapshot, scope_id)
        if not scope.item_ids:
            return WorkSelection(SelectionKind.EMPTY_SCOPE, scope_id, reason_code="empty_scope")
        items = {item.item_id: item for item in snapshot.items}
        incomplete = [
            items[item_id]
            for item_id in scope.item_ids
            if items[item_id].status is WorkItemStatus.PENDING
        ]
        if not incomplete:
            return WorkSelection(
                SelectionKind.SCOPE_COMPLETE,
                scope_id,
                reason_code="all_items_completed",
            )
        blocking: list[str] = []
        for item in incomplete:
            unfinished = [
                dependency
                for dependency in item.dependencies
                if items[dependency].status is not WorkItemStatus.COMPLETED
            ]
            if not unfinished:
                return WorkSelection(
                    SelectionKind.READY,
                    scope_id,
                    item_id=item.item_id,
                    reason_code="dependencies_completed",
                )
            for dependency in unfinished:
                if dependency not in blocking:
                    blocking.append(dependency)
        return WorkSelection(
            SelectionKind.BLOCKED_DEPENDENCIES,
            scope_id,
            blocking_item_ids=tuple(blocking),
            reason_code="incomplete_dependencies",
        )

    def next_ready_scope(
        self, snapshot: WorkSourceSnapshot, parent_scope_id: str
    ) -> ScopeSelection:
        parent = self.get_scope(snapshot, parent_scope_id)
        if not parent.child_scope_ids:
            return ScopeSelection(
                SelectionKind.EMPTY_SCOPE,
                parent_scope_id,
                reason_code="no_child_scopes",
            )
        scopes = {scope.scope_id: scope for scope in snapshot.scopes}
        incomplete_children = [
            scopes[scope_id]
            for scope_id in parent.child_scope_ids
            if scopes[scope_id].status is not WorkScopeStatus.COMPLETED
        ]
        if not incomplete_children:
            return ScopeSelection(
                SelectionKind.SCOPE_COMPLETE,
                parent_scope_id,
                reason_code="all_child_scopes_completed",
            )
        blocking: list[str] = []
        for scope in incomplete_children:
            if scope.status is not WorkScopeStatus.PLANNED:
                continue
            unfinished = [
                dependency
                for dependency in scope.dependencies
                if scopes[dependency].status is not WorkScopeStatus.COMPLETED
            ]
            if unfinished:
                for dependency in unfinished:
                    if dependency not in blocking:
                        blocking.append(dependency)
                continue
            item_selection = self.next_ready_item(snapshot, scope.scope_id)
            if item_selection.kind is SelectionKind.READY:
                return ScopeSelection(
                    SelectionKind.READY,
                    parent_scope_id,
                    scope_id=scope.scope_id,
                    reason_code="scope_and_item_dependencies_completed",
                )
        return ScopeSelection(
            SelectionKind.BLOCKED_DEPENDENCIES,
            parent_scope_id,
            blocking_scope_ids=tuple(blocking),
            reason_code="no_ready_child_scope",
        )

    def build_package(self, snapshot: WorkSourceSnapshot, item_id: str) -> WorkPackage:
        item = self.get_item(snapshot, item_id)
        scope = self.get_scope(snapshot, item.scope_id)
        allowed = []
        seen = set()
        for path in (*item.implementation_paths, *item.test_paths):
            if path.path not in seen:
                allowed.append(path)
                seen.add(path.path)
        return WorkPackage(
            item_id=item.item_id,
            title=item.title,
            scope_id=item.scope_id,
            parent_scope_id=item.parent_scope_id,
            risk=item.risk,
            goal=item.goal,
            acceptance_criteria=item.acceptance_criteria,
            test_requirements=item.test_requirements,
            dependencies=item.dependencies,
            implementation_paths=item.implementation_paths,
            test_paths=item.test_paths,
            allowed_paths=tuple(allowed),
            item_validation_checks=item.validation_checks,
            scope_required_checks=scope.required_checks,
            final_review_required=item.final_review_required,
            parallelizable=item.parallelizable,
            notes=item.notes,
            branch_hint=scope.branch_hint,
            base_branch_hint=scope.base_branch_hint,
            source_location=item.source_location,
            source_revision=snapshot.revision,
        )

    def _collect(self) -> tuple[WorkSourceValidation, WorkSourceSnapshot | None]:
        issues: list[WorkSourceIssue] = []
        documents: dict[str, bytes] = {}
        parents: dict[str, ParentManifest] = {}
        children: dict[str, ChildManifest] = {}
        manifest_order: list[str] = []
        workstreams = self.layout.workstreams_path
        if not workstreams.is_dir():
            self._issue(
                issues,
                "workstreams_directory_missing",
                "configured workstreams directory does not exist",
                self.layout.workstreams_dir,
            )
        else:
            for path in sorted(workstreams.glob("*.yml"), key=lambda value: value.name):
                relative = self._relative(path)
                raw = self._read_document(path, relative, issues)
                if raw is None:
                    continue
                documents[relative] = raw
                try:
                    data = load_yaml(raw)
                except WorkSourceFormatError as exc:
                    self._issue(issues, "invalid_yaml", str(exc), relative)
                    continue
                manifest = parse_manifest(data, SourceLocation(relative), issues)
                if manifest is None:
                    continue
                if manifest.scope_id in parents or manifest.scope_id in children:
                    self._issue(
                        issues,
                        "duplicate_manifest_id",
                        "scope id occurs in multiple manifest files",
                        relative,
                        scope_id=manifest.scope_id,
                    )
                    continue
                manifest_order.append(manifest.scope_id)
                if isinstance(manifest, ParentManifest):
                    parents[manifest.scope_id] = manifest
                else:
                    children[manifest.scope_id] = manifest

        parsed_by_document: dict[str, tuple[ParsedTask, ...]] = {}
        for child in children.values():
            try:
                feature_path, feature_relative = resolve_repository_path(
                    self.layout.repository_root, child.feature
                )
            except WorkSourcePathError as exc:
                self._issue(
                    issues,
                    "unsafe_feature_path",
                    str(exc),
                    child.location.path,
                    scope_id=child.scope_id,
                )
                continue
            if not feature_path.is_dir():
                self._issue(
                    issues,
                    "feature_directory_missing",
                    "feature directory does not exist",
                    child.location.path,
                    scope_id=child.scope_id,
                )
                continue
            tasks_path = feature_path / "tasks.md"
            tasks_relative = f"{feature_relative}/tasks.md"
            if tasks_relative in parsed_by_document:
                continue
            raw = self._read_document(tasks_path, tasks_relative, issues)
            if raw is None:
                continue
            documents[tasks_relative] = raw
            parsed_by_document[tasks_relative] = parse_tasks_document(
                raw,
                tasks_relative,
                self.layout.repository_root,
                issues,
            )

        parsed_tasks = tuple(
            task for path in sorted(parsed_by_document) for task in parsed_by_document[path]
        )
        items: dict[str, WorkItem] = {}
        for parsed in parsed_tasks:
            item = parsed.item
            if item.item_id in items:
                self._issue(
                    issues,
                    "duplicate_item_id",
                    "item id occurs in multiple tasks documents",
                    item.source_location.path,
                    item.source_location.line,
                    item_id=item.item_id,
                )
                continue
            items[item.item_id] = item

        self._validate_scope_consistency(parents, children, issues)
        self._validate_item_consistency(children, items, issues)
        self._validate_dependency_graphs(children, items, issues)
        active_scope = self._read_active_scope(children, documents, issues)
        validation = WorkSourceValidation.from_issues(issues)
        if not validation.ok:
            return validation, None

        scopes = self._build_scopes(manifest_order, parents, children, issues)
        if issues:
            return WorkSourceValidation.from_issues(issues), None
        revision = _build_revision(documents)
        snapshot = WorkSourceSnapshot(
            scopes,
            tuple(items[item_id] for item_id in sorted(items)),
            revision,
            active_scope,
        )
        return validation, snapshot

    def _build_scopes(
        self,
        manifest_order: list[str],
        parents: dict[str, ParentManifest],
        children: dict[str, ChildManifest],
        issues: list[WorkSourceIssue],
    ) -> tuple[WorkScope, ...]:
        scopes = []
        for scope_id in manifest_order:
            if scope_id in parents:
                manifest = parents[scope_id]
                scopes.append(
                    WorkScope(
                        manifest.scope_id,
                        None,
                        manifest.title,
                        manifest.status,
                        None,
                        manifest.goal,
                        manifest.child_ids,
                        (),
                        (),
                        (),
                        None,
                        None,
                        None,
                        manifest.location,
                    )
                )
                continue
            manifest = children[scope_id]
            checks = []
            for declaration in manifest.required_check_declarations:
                try:
                    checks.extend(
                        parse_validation_checks(
                            declaration,
                            origin=ValidationOrigin.SCOPE,
                            source_location=manifest.location,
                        )
                    )
                except WorkSourceFormatError as exc:
                    self._issue(
                        issues,
                        "invalid_required_check",
                        str(exc),
                        manifest.location.path,
                        scope_id=manifest.scope_id,
                    )
            scopes.append(
                WorkScope(
                    manifest.scope_id,
                    manifest.parent_id,
                    manifest.title,
                    manifest.status,
                    manifest.risk,
                    "",
                    (),
                    manifest.item_ids,
                    manifest.dependencies,
                    tuple(checks),
                    manifest.policy_hints,
                    manifest.branch,
                    manifest.base_branch,
                    manifest.location,
                )
            )
        return tuple(scopes)

    def _validate_scope_consistency(
        self,
        parents: dict[str, ParentManifest],
        children: dict[str, ChildManifest],
        issues: list[WorkSourceIssue],
    ) -> None:
        for child in children.values():
            parent = parents.get(child.parent_id)
            if parent is None:
                self._issue(
                    issues,
                    "unknown_parent_scope",
                    "child scope references an unknown parent",
                    child.location.path,
                    scope_id=child.scope_id,
                )
            elif child.scope_id not in parent.child_ids:
                self._issue(
                    issues,
                    "child_omitted_by_parent",
                    "child scope is not declared by its parent",
                    child.location.path,
                    scope_id=child.scope_id,
                )
            for dependency in child.dependencies:
                if dependency not in children:
                    self._issue(
                        issues,
                        "unknown_scope_dependency",
                        "scope dependency does not exist",
                        child.location.path,
                        scope_id=child.scope_id,
                    )
        for parent in parents.values():
            for child_id in parent.child_ids:
                child = children.get(child_id)
                if child is None:
                    self._issue(
                        issues,
                        "parent_lists_unknown_child",
                        "parent declares an unknown child scope",
                        parent.location.path,
                        scope_id=parent.scope_id,
                    )
                elif child.parent_id != parent.scope_id:
                    self._issue(
                        issues,
                        "child_parent_mismatch",
                        "declared child points to a different parent",
                        parent.location.path,
                        scope_id=child_id,
                    )

    def _validate_item_consistency(
        self,
        children: dict[str, ChildManifest],
        items: dict[str, WorkItem],
        issues: list[WorkSourceIssue],
    ) -> None:
        owners: dict[str, list[str]] = defaultdict(list)
        for child in children.values():
            for item_id in child.item_ids:
                owners[item_id].append(child.scope_id)
                if item_id not in items:
                    self._issue(
                        issues,
                        "manifest_lists_unknown_item",
                        "scope manifest declares an unknown item",
                        child.location.path,
                        scope_id=child.scope_id,
                        item_id=item_id,
                    )
        for item in items.values():
            declared = owners.get(item.item_id, [])
            if not declared:
                self._issue(
                    issues,
                    "item_omitted_from_manifest",
                    "item is not owned by any scope manifest",
                    item.source_location.path,
                    item.source_location.line,
                    item_id=item.item_id,
                )
                continue
            if len(declared) > 1:
                self._issue(
                    issues,
                    "item_owned_by_multiple_scopes",
                    "item is declared by multiple scope manifests",
                    item.source_location.path,
                    item.source_location.line,
                    item_id=item.item_id,
                )
                continue
            owner_id = declared[0]
            owner = children[owner_id]
            if item.scope_id not in children:
                self._issue(
                    issues,
                    "unknown_item_scope",
                    "item references an unknown owner scope",
                    item.source_location.path,
                    item.source_location.line,
                    item_id=item.item_id,
                )
            elif item.scope_id != owner_id:
                self._issue(
                    issues,
                    "item_scope_mismatch",
                    "item owner field disagrees with manifest ownership",
                    item.source_location.path,
                    item.source_location.line,
                    item_id=item.item_id,
                )
            if item.parent_scope_id != owner.parent_id:
                self._issue(
                    issues,
                    "item_parent_mismatch",
                    "item parent field disagrees with owner scope",
                    item.source_location.path,
                    item.source_location.line,
                    item_id=item.item_id,
                )

    def _validate_dependency_graphs(
        self,
        children: dict[str, ChildManifest],
        items: dict[str, WorkItem],
        issues: list[WorkSourceIssue],
    ) -> None:
        scope_graph = {scope_id: child.dependencies for scope_id, child in children.items()}
        for cycle in _cycles(scope_graph):
            child = children[cycle[0]]
            self._issue(
                issues,
                "scope_dependency_cycle",
                f"scope dependency cycle: {' -> '.join(cycle)}",
                child.location.path,
                scope_id=cycle[0],
            )
        item_graph = {item_id: item.dependencies for item_id, item in items.items()}
        for item in items.values():
            for dependency in item.dependencies:
                if dependency not in items:
                    self._issue(
                        issues,
                        "unknown_item_dependency",
                        "item dependency does not exist",
                        item.source_location.path,
                        item.source_location.line,
                        item_id=item.item_id,
                    )
        for cycle in _cycles(item_graph):
            item = items[cycle[0]]
            self._issue(
                issues,
                "item_dependency_cycle",
                f"item dependency cycle: {' -> '.join(cycle)}",
                item.source_location.path,
                item.source_location.line,
                item_id=cycle[0],
            )
        owners = {
            item_id: child.scope_id
            for child in children.values()
            for item_id in child.item_ids
            if item_id in items
        }
        for item in items.values():
            owner = owners.get(item.item_id)
            if owner is None:
                continue
            authorized = _reachable(scope_graph, owner)
            for dependency in item.dependencies:
                dependency_owner = owners.get(dependency)
                if (
                    dependency_owner is not None
                    and dependency_owner != owner
                    and dependency_owner not in authorized
                ):
                    self._issue(
                        issues,
                        "cross_scope_dependency_not_declared",
                        "cross-scope item dependency lacks scope dependency authorization",
                        item.source_location.path,
                        item.source_location.line,
                        item_id=item.item_id,
                    )

    def _read_active_scope(
        self,
        children: dict[str, ChildManifest],
        documents: dict[str, bytes],
        issues: list[WorkSourceIssue],
    ) -> str | None:
        path = self.layout.active_scope_path
        relative = self.layout.active_scope_file
        if path is None or relative is None or not path.exists():
            return None
        raw = self._read_document(path, relative, issues)
        if raw is None:
            return None
        documents[relative] = raw
        try:
            active = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            self._issue(issues, "active_scope_not_utf8", "active scope must be UTF-8", relative)
            return None
        if not active:
            self._issue(issues, "active_scope_empty", "active scope file is empty", relative)
            return None
        if CHILD_ID.fullmatch(active) is None:
            self._issue(issues, "active_scope_malformed", "active scope id is malformed", relative)
            return None
        if active not in children:
            self._issue(issues, "active_scope_unknown", "active scope id does not exist", relative)
            return None
        return active

    def _read_document(
        self,
        path: Path,
        relative: str,
        issues: list[WorkSourceIssue],
    ) -> bytes | None:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.layout.repository_root)
        except ValueError:
            self._issue(issues, "source_symlink_escape", "source path escapes repository", relative)
            return None
        if not resolved.is_file():
            self._issue(issues, "source_document_missing", "source document is missing", relative)
            return None
        try:
            raw = resolved.read_bytes()
        except OSError:
            self._issue(
                issues, "source_document_unreadable", "source document is unreadable", relative
            )
            return None
        if len(raw) > _MAX_SOURCE_BYTES:
            self._issue(
                issues, "source_document_too_large", "source document exceeds size limit", relative
            )
            return None
        return raw

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.layout.repository_root).as_posix()

    @staticmethod
    def _issue(
        issues: list[WorkSourceIssue],
        code: str,
        message: str,
        path: str,
        line: int | None = None,
        *,
        scope_id: str | None = None,
        item_id: str | None = None,
    ) -> None:
        issues.append(
            WorkSourceIssue(
                code,
                IssueSeverity.ERROR,
                message,
                SourceLocation(path, line),
                scope_id,
                item_id,
            )
        )


def _cycles(graph: dict[str, tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    cycles: list[tuple[str, ...]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dependency in graph.get(node, ()):
            if dependency not in graph:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycle = tuple((*stack[start:], dependency))
                if cycle not in cycles:
                    cycles.append(cycle)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            visit(node)
    return tuple(cycles)


def _reachable(graph: dict[str, tuple[str, ...]], start: str) -> set[str]:
    reached: set[str] = set()
    pending = list(reversed(graph.get(start, ())))
    while pending:
        node = pending.pop()
        if node in reached:
            continue
        reached.add(node)
        pending.extend(reversed(graph.get(node, ())))
    return reached


def _build_revision(documents: dict[str, bytes]) -> WorkSourceRevision:
    revisions = tuple(
        SourceDocumentRevision(
            path,
            f"sha256:{hashlib.sha256(raw).hexdigest()}",
            len(raw),
        )
        for path, raw in sorted(documents.items())
    )
    digest = hashlib.sha256()
    for document in revisions:
        digest.update(document.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(document.sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(document.size_bytes).encode("ascii"))
        digest.update(b"\n")
    return WorkSourceRevision(revisions, f"sha256:{digest.hexdigest()}")
