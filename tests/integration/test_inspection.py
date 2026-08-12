from __future__ import annotations

import shutil

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.infra import GitAdapter
from agentgraph.integration import ShadowOutcome, ShadowRequest
from agentgraph.runtime import ProjectRegistry, RuntimePaths

from .conftest import initialize_target, make_runner


def test_non_git_target_is_invalid_before_registration(tmp_path) -> None:
    root = tmp_path / "target"
    root.mkdir()
    runtime = tmp_path / "runtime"
    source_root = tmp_path / "source"
    initialize_target(source_root)
    runner = make_runner(
        root,
        runtime,
        work_source=SpecKitAdapter(SpecKitLayout(source_root)),
    )

    report = runner.run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.INVALID_PROJECT
    assert report.graph_state is None
    assert report.issues[0].code == "git_repository_missing"
    assert not (runtime / "registry.json").exists()


def test_configured_subdirectory_does_not_expand_to_git_root(target, tmp_path) -> None:
    subdirectory = target / "specs"
    runner = make_runner(
        subdirectory,
        tmp_path / "runtime",
        work_source=SpecKitAdapter(SpecKitLayout(target)),
    )

    report = runner.run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.INVALID_PROJECT
    assert report.issues[0].code == "repository_root_mismatch"


def test_invalid_work_source_never_creates_graph_state(target, tmp_path) -> None:
    tasks = target / "specs" / "one" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace("Epic: E001\n", ""),
        encoding="utf-8",
    )

    report = make_runner(target, tmp_path / "runtime").run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.INVALID_SOURCE
    assert report.graph_state is None
    assert "missing_task_field" in {issue.code for issue in report.issues}


def test_project_identity_is_stable_per_canonical_root_and_distinct_for_copy(
    target, tmp_path
) -> None:
    runtime_paths = RuntimePaths.resolve(tmp_path / "runtime")
    identifiers = iter(("prj_first", "prj_second"))
    registry = ProjectRegistry(runtime_paths, project_id_factory=lambda: next(identifiers))
    executable = shutil.which("git") or "git"
    first = __import__("agentgraph.integration", fromlist=["ShadowRunner"]).ShadowRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        git_adapter=GitAdapter(executable=executable),
        project_registry=registry,
        run_id_factory=lambda _: "shadow_test",
    )

    first_report = first.run(ShadowRequest(scope_id="E001"))
    repeated_report = first.run(ShadowRequest(scope_id="E001"))
    copied = tmp_path / "copied"
    shutil.copytree(target, copied)
    second = __import__("agentgraph.integration", fromlist=["ShadowRunner"]).ShadowRunner(
        copied,
        SpecKitAdapter(SpecKitLayout(copied)),
        git_adapter=GitAdapter(executable=executable),
        project_registry=registry,
        run_id_factory=lambda _: "shadow_test",
    )
    copied_report = second.run(ShadowRequest(scope_id="E001"))

    assert first_report.project_id == repeated_report.project_id == "prj_first"
    assert first_report.input_fingerprint == repeated_report.input_fingerprint
    assert copied_report.project_id == "prj_second"
    assert copied_report.input_fingerprint != first_report.input_fingerprint
