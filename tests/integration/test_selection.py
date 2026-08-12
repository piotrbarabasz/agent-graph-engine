from __future__ import annotations

from agentgraph.integration import ShadowOutcome, ShadowRequest

from .conftest import initialize_target, make_runner


def test_parent_selection_preserves_declared_child_order(tmp_path) -> None:
    root = tmp_path / "target"
    initialize_target(root, multi_scope=True, parent_order=("E002", "E001"))

    report = make_runner(root, tmp_path / "runtime").run(ShadowRequest(parent_scope_id="M001"))

    assert report.outcome is ShadowOutcome.READY_FOR_EXPLORE
    assert report.selected_scope_id == "E002"
    assert report.selected_item_id == "T002"


def test_dependency_blocked_selection_does_not_enter_explore(tmp_path) -> None:
    root = tmp_path / "target"
    initialize_target(root, multi_scope=True, blocked_second=True)

    report = make_runner(root, tmp_path / "runtime").run(ShadowRequest(scope_id="E002"))

    assert report.outcome is ShadowOutcome.BLOCKED
    assert report.selected_scope_id == "E002"
    assert report.executed_nodes[-2:] == ("SELECT_WORK", "FINALIZE")
    assert "EXPLORE" not in report.executed_nodes
    issue = next(issue for issue in report.issues if issue.code == "blocked_item_dependencies")
    assert issue.related_ids == ("T001",)


def test_unknown_parent_scope_is_a_typed_blocker(target, tmp_path) -> None:
    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(parent_scope_id="M999"))

    assert report.outcome is ShadowOutcome.BLOCKED
    assert "unknown_parent_scope" in {issue.code for issue in report.issues}
