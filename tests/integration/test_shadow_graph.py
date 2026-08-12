from __future__ import annotations

from agentgraph.core import RunStatus
from agentgraph.integration import BranchDisposition, ShadowOutcome, ShadowRequest

from .conftest import make_runner


def test_ready_vertical_slice_reaches_explore_boundary_and_projects_state(target, tmp_path) -> None:
    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.READY_FOR_EXPLORE
    assert report.branch_disposition is BranchDisposition.PREPARATION_REQUIRED
    assert report.executed_nodes == ("START", "DISCOVER_PROJECT", "PREFLIGHT", "SELECT_WORK")
    assert "EXPLORE" not in report.executed_nodes
    assert report.graph_state is not None
    state = report.graph_state
    assert state.graph.current_node == "EXPLORE"
    assert state.run.status is RunStatus.RUNNING
    assert state.repository.identifier == report.project_id
    assert state.baseline.revision == report.head_sha
    assert state.baseline.metadata["work_source_revision"] == report.work_source_revision
    assert state.work.item is not None and state.work.item.id == "T001"
    assert state.work.completed_items == ()
    assert state.work.dependencies == ()
    assert state.work.delivery_scope is not None
    assert state.work.delivery_scope.allowed_paths == (
        "src/t001.py",
        "tests/t001.py",
    )
    assert report.work_package is not None
    assert report.work_package.source_revision.fingerprint == report.work_source_revision
    assert report.selected_item_id == state.work.item.id
    assert report.selected_scope_id == state.work.delivery_scope.id
    assert not (target / "executed.txt").exists()


def test_completed_scope_follows_canonical_no_work_path(target, tmp_path) -> None:
    manifest = target / ".specify" / "workstreams" / "E001.yml"
    tasks = target / "specs" / "one" / "tasks.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("status: planned", "status: completed"),
        encoding="utf-8",
    )
    tasks.write_text(tasks.read_text(encoding="utf-8").replace("- [ ]", "- [X]"), encoding="utf-8")
    from .conftest import git

    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "complete")

    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.NO_WORK
    assert report.graph_state is not None
    assert report.graph_state.graph.current_node == "END"
    assert report.graph_state.run.status is RunStatus.COMPLETED
    assert report.graph_state.work.item is None
    assert report.graph_state.work.completed_items == ()
    assert report.executed_nodes == (
        "START",
        "DISCOVER_PROJECT",
        "PREFLIGHT",
        "SELECT_WORK",
        "FINALIZE",
    )


def test_no_selection_anchor_is_not_guessed(target, tmp_path) -> None:
    report = make_runner(target, tmp_path / "runtime").run()

    assert report.outcome is ShadowOutcome.SELECTION_REQUIRED
    assert report.selected_scope_id is None
    assert report.graph_state is not None
    assert report.graph_state.graph.current_node == "END"
    assert report.graph_state.run.status is RunStatus.BLOCKED
    assert {issue.code for issue in report.issues} == {"selection_required"}
