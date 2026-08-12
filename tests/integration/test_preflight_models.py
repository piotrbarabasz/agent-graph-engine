from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.integration import (
    ShadowRequest,
    assess_preflight,
    inspect_project,
    prepare_selection,
)
from agentgraph.runtime import ProjectRegistry, RuntimePaths

from .conftest import make_runner


def _prepared(target, tmp_path):
    source = SpecKitAdapter(SpecKitLayout(target))
    runner = make_runner(target, tmp_path / "unused")
    inspection = inspect_project(
        target,
        git_adapter=runner.git_adapter,
        project_registry=ProjectRegistry(
            RuntimePaths.resolve(tmp_path / "runtime"),
            project_id_factory=lambda: "prj_preflight",
        ),
        work_source=source,
    )
    selection, _ = prepare_selection(source, inspection.work_snapshot, ShadowRequest("E001"))
    return inspection, selection


def test_conflicts_are_an_explicit_preflight_blocker(target, tmp_path) -> None:
    inspection, selection = _prepared(target, tmp_path)
    git = replace(
        inspection.git_snapshot,
        conflicted_paths=(Path("conflicted.py"),),
        dirty=True,
    )

    assessment = assess_preflight(replace(inspection, git_snapshot=git), selection)

    assert not assessment.ready
    assert assessment.issues[0].code == "conflicts_present"
    assert "dirty_worktree" in {issue.code for issue in assessment.issues}


def test_unborn_head_is_an_explicit_preflight_blocker(target, tmp_path) -> None:
    inspection, selection = _prepared(target, tmp_path)
    git = replace(inspection.git_snapshot, head_sha=None)

    assessment = assess_preflight(replace(inspection, git_snapshot=git), selection)

    assert not assessment.ready
    assert assessment.issues[0].code == "unborn_head"
