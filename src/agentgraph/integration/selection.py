"""Deterministic neutral scope and work-item resolution."""

from __future__ import annotations

from agentgraph.work import (
    SelectionKind,
    WorkPackage,
    WorkScopeNotFoundError,
    WorkScopeStatus,
    WorkSource,
    WorkSourceSnapshot,
)

from .errors import ShadowSelectionError
from .models import (
    IntegrationIssue,
    IntegrationIssueSeverity,
    SelectionDisposition,
    SelectionPlan,
    ShadowRequest,
)


def prepare_selection(
    source: WorkSource,
    snapshot: WorkSourceSnapshot,
    request: ShadowRequest,
) -> tuple[SelectionPlan, WorkPackage | None]:
    """Resolve an explicit or active anchor without scanning unrelated scopes."""

    active_id = snapshot.active_scope_id
    if request.scope_id is not None and active_id is not None and request.scope_id != active_id:
        issue = _issue("active_scope_conflict", "explicit scope conflicts with active scope")
        return SelectionPlan(SelectionDisposition.BLOCKED, issues=(issue,)), None

    scope_id = request.scope_id
    if request.parent_scope_id is not None:
        try:
            parent_selection = source.next_ready_scope(snapshot, request.parent_scope_id)
        except WorkScopeNotFoundError:
            issue = _issue(
                "unknown_parent_scope",
                "selected parent scope does not exist",
                (request.parent_scope_id,),
            )
            return SelectionPlan(SelectionDisposition.BLOCKED, issues=(issue,)), None
        if parent_selection.kind is SelectionKind.READY:
            scope_id = parent_selection.scope_id
        elif parent_selection.kind in {SelectionKind.SCOPE_COMPLETE, SelectionKind.EMPTY_SCOPE}:
            return (
                SelectionPlan(
                    SelectionDisposition.NO_WORK,
                    reason_code=parent_selection.reason_code,
                ),
                None,
            )
        else:
            issue = _issue(
                "blocked_scope_dependencies",
                "no child scope is dependency-ready",
                parent_selection.blocking_scope_ids,
            )
            return (
                SelectionPlan(
                    SelectionDisposition.BLOCKED,
                    reason_code=parent_selection.reason_code,
                    issues=(issue,),
                    blocking_ids=parent_selection.blocking_scope_ids,
                ),
                None,
            )
    elif scope_id is None:
        scope_id = active_id

    if scope_id is None:
        issue = _issue("selection_required", "an explicit or active scope is required")
        return SelectionPlan(SelectionDisposition.SELECTION_REQUIRED, issues=(issue,)), None

    try:
        scope = source.get_scope(snapshot, scope_id)
    except WorkScopeNotFoundError:
        issue = _issue("unknown_scope", "selected scope does not exist", (scope_id,))
        return SelectionPlan(SelectionDisposition.BLOCKED, scope_id, issues=(issue,)), None

    if active_id == scope_id and scope.status is not WorkScopeStatus.ACTIVE:
        issue = _issue(
            "active_scope_status_mismatch",
            "active scope evidence requires ACTIVE source status",
            (scope_id,),
        )
        return SelectionPlan(SelectionDisposition.BLOCKED, scope_id, issues=(issue,)), None
    if scope.status is WorkScopeStatus.COMPLETED:
        return (
            SelectionPlan(SelectionDisposition.NO_WORK, scope_id, reason_code="scope_completed"),
            None,
        )
    if scope.status in {WorkScopeStatus.BLOCKED, WorkScopeStatus.REVIEW}:
        issue = _issue(
            "scope_not_eligible",
            "selected scope status does not permit new work",
            (scope_id,),
        )
        return SelectionPlan(SelectionDisposition.BLOCKED, scope_id, issues=(issue,)), None

    selection = source.next_ready_item(snapshot, scope_id)
    if selection.kind is SelectionKind.READY:
        assert selection.item_id is not None
        package = source.build_package(snapshot, selection.item_id)
        plan = SelectionPlan(
            SelectionDisposition.READY,
            scope_id,
            selection.item_id,
            selection.reason_code,
        )
        _reconcile_ready_package(snapshot, plan, package)
        return plan, package
    if selection.kind in {SelectionKind.SCOPE_COMPLETE, SelectionKind.EMPTY_SCOPE}:
        return (
            SelectionPlan(
                SelectionDisposition.NO_WORK,
                scope_id,
                reason_code=selection.reason_code,
            ),
            None,
        )
    issue = _issue(
        "blocked_item_dependencies",
        "no pending work item is dependency-ready",
        selection.blocking_item_ids,
    )
    return (
        SelectionPlan(
            SelectionDisposition.BLOCKED,
            scope_id,
            reason_code=selection.reason_code,
            issues=(issue,),
            blocking_ids=selection.blocking_item_ids,
        ),
        None,
    )


def _issue(code: str, message: str, related_ids: tuple[str, ...] = ()) -> IntegrationIssue:
    return IntegrationIssue(code, IntegrationIssueSeverity.ERROR, message, related_ids)


def _reconcile_ready_package(
    snapshot: WorkSourceSnapshot,
    selection: SelectionPlan,
    package: WorkPackage,
) -> None:
    """Bind a package to the exact selected item, scope, hierarchy, and revision."""

    if package.item_id != selection.item_id:
        raise ShadowSelectionError("work package item differs from selected item")
    if package.scope_id != selection.scope_id:
        raise ShadowSelectionError("work package scope differs from selected scope")
    if package.source_revision.fingerprint != snapshot.revision.fingerprint:
        raise ShadowSelectionError("work package source revision is stale")
    item = next((value for value in snapshot.items if value.item_id == package.item_id), None)
    if item is None or item.scope_id != package.scope_id:
        raise ShadowSelectionError("work package item ownership differs from snapshot")
    scope = next((value for value in snapshot.scopes if value.scope_id == package.scope_id), None)
    if scope is None:
        raise ShadowSelectionError("work package scope is absent from snapshot")
    if scope.parent_scope_id is not None and package.parent_scope_id != scope.parent_scope_id:
        raise ShadowSelectionError("work package parent scope differs from snapshot hierarchy")
