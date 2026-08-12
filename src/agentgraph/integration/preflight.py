"""Typed read-only repository and branch readiness assessment."""

from __future__ import annotations

from agentgraph.work import WorkScopeStatus

from .models import (
    BranchDisposition,
    IntegrationIssue,
    IntegrationIssueSeverity,
    PreflightAssessment,
    ProjectInspection,
    SelectionDisposition,
    SelectionPlan,
)


def assess_preflight(
    inspection: ProjectInspection,
    selection: SelectionPlan,
) -> PreflightAssessment:
    """Evaluate Git and selected-scope safety without changing either source."""

    git = inspection.git_snapshot
    issues: list[IntegrationIssue] = []
    if git.head_sha is None:
        issues.append(_issue("unborn_head", "repository HEAD has no commit"))
    if git.detached_head:
        issues.append(_issue("detached_head", "detached HEAD is not execution-ready"))
    if git.conflicted_paths:
        issues.append(
            _issue(
                "conflicts_present",
                "repository contains unresolved conflicts",
                tuple(path.as_posix() for path in git.conflicted_paths),
            )
        )
    if git.dirty:
        dirty_paths = tuple(
            dict.fromkeys(
                path.as_posix()
                for paths in (
                    git.staged_paths,
                    git.unstaged_paths,
                    git.untracked_paths,
                    git.conflicted_paths,
                )
                for path in paths
            )
        )
        issues.append(_issue("dirty_worktree", "repository working tree is not clean", dirty_paths))

    disposition = BranchDisposition.NOT_APPLICABLE
    if selection.scope_id is not None:
        scope = next(
            (
                item
                for item in inspection.work_snapshot.scopes
                if item.scope_id == selection.scope_id
            ),
            None,
        )
        if scope is None:
            disposition = BranchDisposition.BLOCKED
        elif scope.status is WorkScopeStatus.COMPLETED:
            disposition = BranchDisposition.NOT_APPLICABLE
        elif scope.status is WorkScopeStatus.ACTIVE:
            if git.branch == scope.branch_hint:
                disposition = BranchDisposition.ALIGNED
            else:
                disposition = BranchDisposition.BLOCKED
                issues.append(
                    _issue(
                        "active_scope_branch_mismatch",
                        "ACTIVE scope requires its declared branch",
                        (selection.scope_id,),
                    )
                )
        elif scope.status is WorkScopeStatus.PLANNED:
            if git.branch == scope.branch_hint:
                disposition = BranchDisposition.ALIGNED
            elif git.branch == scope.base_branch_hint:
                disposition = BranchDisposition.PREPARATION_REQUIRED
            else:
                disposition = BranchDisposition.BLOCKED
                issues.append(
                    _issue(
                        "branch_context_conflict",
                        "current branch matches neither scope nor base branch hint",
                        (selection.scope_id,),
                    )
                )
        else:
            disposition = BranchDisposition.BLOCKED

    if (
        selection.disposition is SelectionDisposition.BLOCKED
        and selection.issues
        and selection.issues[0].code
        in {
            "active_scope_conflict",
            "active_scope_status_mismatch",
            "scope_not_eligible",
            "unknown_scope",
            "unknown_parent_scope",
        }
    ):
        issues.extend(selection.issues)
        disposition = BranchDisposition.BLOCKED

    return PreflightAssessment(not issues, tuple(issues), disposition)


def _issue(code: str, message: str, related_ids: tuple[str, ...] = ()) -> IntegrationIssue:
    return IntegrationIssue(code, IntegrationIssueSeverity.ERROR, message, related_ids)
