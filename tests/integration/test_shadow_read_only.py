from __future__ import annotations

from agentgraph.integration import ShadowOutcome, ShadowRequest
from agentgraph.runtime import RuntimePaths

from .conftest import make_runner, semantic_git_state, working_tree_bytes


def test_shadow_changes_external_registry_but_not_target(target, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    before_git = semantic_git_state(target)
    before_tree = working_tree_bytes(target)

    report = make_runner(target, runtime_root).run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.READY_FOR_EXPLORE
    assert semantic_git_state(target) == before_git
    assert working_tree_bytes(target) == before_tree
    assert (runtime_root / "registry.json").exists()
    assert report.project_id is not None
    paths = RuntimePaths.resolve(runtime_root)
    assert not paths.active_run(report.project_id).exists()
    assert tuple(paths.project(report.project_id).joinpath("runs").iterdir()) == ()


def test_declared_validation_command_is_never_executed(target, tmp_path) -> None:
    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.work_package is not None
    assert report.work_package.item_validation_checks[0].argv[0] == "python"
    assert not (target / "executed.txt").exists()
