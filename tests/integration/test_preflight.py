from __future__ import annotations

import pytest

from agentgraph.core import RunStatus
from agentgraph.integration import BranchDisposition, ShadowOutcome, ShadowRequest
from agentgraph.integration.errors import ShadowRequestError

from .conftest import git, initialize_target, make_runner


def test_active_scope_on_aligned_branch_is_ready(tmp_path) -> None:
    root = tmp_path / "target"
    initialize_target(root, status="active", active_scope="E001")
    git(root, "switch", "--quiet", "-c", "work/e001")

    report = make_runner(root, tmp_path / "runtime").run()

    assert report.outcome is ShadowOutcome.READY_FOR_EXPLORE
    assert report.branch_disposition is BranchDisposition.ALIGNED
    assert report.selected_scope_id == "E001"


def test_active_scope_on_wrong_branch_is_blocked_at_preflight(tmp_path) -> None:
    root = tmp_path / "target"
    initialize_target(root, status="active", active_scope="E001")

    report = make_runner(root, tmp_path / "runtime").run()

    assert report.outcome is ShadowOutcome.BLOCKED
    assert report.branch_disposition is BranchDisposition.BLOCKED
    assert report.graph_state is not None
    assert report.graph_state.graph.current_node == "END"
    assert report.graph_state.run.status is RunStatus.BLOCKED
    assert report.executed_nodes == ("START", "DISCOVER_PROJECT", "PREFLIGHT", "FINALIZE")
    assert report.issues[0].code == "active_scope_branch_mismatch"


def test_dirty_tracked_and_untracked_trees_are_blocked_without_cleanup(target, tmp_path) -> None:
    tracked = target / "tracked.txt"
    tracked.write_text("modified\n", encoding="utf-8")
    untracked = target / "untracked.txt"
    untracked.write_text("preserve\n", encoding="utf-8")

    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.BLOCKED
    assert "dirty_worktree" in {issue.code for issue in report.issues}
    assert tracked.read_text(encoding="utf-8") == "modified\n"
    assert untracked.read_text(encoding="utf-8") == "preserve\n"


def test_detached_head_is_blocked(target, tmp_path) -> None:
    git(target, "checkout", "--quiet", "--detach", "HEAD")

    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.BLOCKED
    assert report.issues[0].code == "detached_head"


def test_active_scope_conflict_fails_closed(tmp_path) -> None:
    root = tmp_path / "target"
    initialize_target(root, status="active", active_scope="E001", multi_scope=True)

    report = make_runner(root, tmp_path / "runtime").run(ShadowRequest(scope_id="E002"))

    assert report.outcome is ShadowOutcome.BLOCKED
    assert "active_scope_conflict" in {issue.code for issue in report.issues}


def test_active_scope_status_mismatch_fails_closed(tmp_path) -> None:
    root = tmp_path / "target"
    initialize_target(root, status="planned", active_scope="E001")

    report = make_runner(root, tmp_path / "runtime").run()

    assert report.outcome is ShadowOutcome.BLOCKED
    assert "active_scope_status_mismatch" in {issue.code for issue in report.issues}


def test_planned_scope_on_declared_branch_is_aligned(target, tmp_path) -> None:
    git(target, "switch", "--quiet", "-c", "work/e001")

    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.READY_FOR_EXPLORE
    assert report.branch_disposition is BranchDisposition.ALIGNED


def test_planned_scope_on_unrelated_branch_is_blocked(target, tmp_path) -> None:
    git(target, "switch", "--quiet", "-c", "unrelated")

    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.BLOCKED
    assert "branch_context_conflict" in {issue.code for issue in report.issues}


def test_request_rejects_two_explicit_selectors() -> None:
    with pytest.raises(ShadowRequestError):
        ShadowRequest(scope_id="scope", parent_scope_id="parent")
