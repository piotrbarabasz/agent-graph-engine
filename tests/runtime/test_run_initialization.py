from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.runtime.errors import ActiveRunExistsError, IncompleteRunInitializationError
from agentgraph.runtime.project_registry import ProjectRegistry
from tests.runtime.test_coordinator import coordinator


@pytest.mark.parametrize(
    "crash_stage",
    [
        "after_run_staging_creation",
        "after_state_initialization",
        "after_journal_creation",
        "after_run_started",
        "before_run_promotion",
    ],
)
def test_initialization_crash_preserves_evidence_and_explicit_retry_is_usable(
    runtime_paths, project, crash_stage: str
) -> None:
    target = Path(project.canonical_root)
    marker = target / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob("*")}

    def fault(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError("initialization crash")

    runtime, _ = coordinator(runtime_paths, project, fault=fault)
    run_id = f"run_init_{crash_stage}"
    with pytest.raises(RuntimeError, match="initialization"):
        runtime.start_run(run_id)

    canonical = runtime_paths.run(project.project_id, run_id)
    staging = runtime_paths.initializing_run(project.project_id, run_id)
    assert not canonical.exists()
    assert staging.exists()
    assert not runtime_paths.active_run(project.project_id).exists()
    with pytest.raises(IncompleteRunInitializationError):
        runtime.open_session(run_id)
    runtime.fault = lambda stage: None
    with pytest.raises(IncompleteRunInitializationError):
        runtime.start_run(run_id)

    handle = runtime.recover_incomplete_run_initialization(run_id)
    assert handle.runtime_path == canonical
    assert not staging.exists()
    assert list(runtime_paths.initialization_recovery(project.project_id).glob(f"{run_id}-*"))
    with runtime.open_session(run_id) as session:
        assert session.store.load().graph.current_node == "START"
    assert ProjectRegistry(runtime_paths).get(project.project_id) == project
    after = {path.relative_to(target): path.read_bytes() for path in target.rglob("*")}
    assert after == before


def test_crash_after_promotion_is_discovered_as_the_only_unfinished_run(
    runtime_paths, project
) -> None:
    def fault(stage: str) -> None:
        if stage == "after_run_promotion":
            raise RuntimeError("promotion crash")

    runtime, _ = coordinator(runtime_paths, project, fault=fault)
    with pytest.raises(RuntimeError, match="promotion"):
        runtime.start_run("run_promoted")
    assert runtime_paths.run(project.project_id, "run_promoted").is_dir()
    assert not runtime_paths.active_run(project.project_id).exists()

    runtime.fault = lambda stage: None
    with pytest.raises(ActiveRunExistsError, match="unfinished active run"):
        runtime.start_run("run_other")
    with runtime.open_session("run_promoted") as session:
        assert session.store.load().graph.current_node == "START"


@pytest.mark.parametrize(
    ("crash_stage", "promoted"),
    [
        ("after_run_started", False),
        ("after_initialization_artifacts", False),
        ("before_run_promotion", False),
        ("after_run_promotion", True),
        ("before_run_activation", True),
        ("after_run_activation", True),
    ],
)
def test_initialization_artifact_is_present_in_every_promoted_run(
    runtime_paths, project, crash_stage: str, promoted: bool
) -> None:
    def fault(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError("artifact initialization crash")

    runtime, _ = coordinator(runtime_paths, project, fault=fault)
    run_id = f"run_artifact_{crash_stage}"
    with pytest.raises(RuntimeError, match="artifact"):
        runtime.start_run(
            run_id,
            initialize_artifacts=lambda staging: atomic_write_bytes(
                staging / "caller.json", b'{"ready":true}'
            ),
        )

    canonical = runtime_paths.run(project.project_id, run_id)
    staging = runtime_paths.initializing_run(project.project_id, run_id)
    assert canonical.exists() is promoted
    if promoted:
        assert (canonical / "caller.json").read_bytes() == b'{"ready":true}'
    else:
        assert staging.is_dir()
        assert not runtime_paths.active_run(project.project_id).exists()


def test_initialization_artifact_failure_never_promotes_or_activates(
    runtime_paths, project
) -> None:
    runtime, _ = coordinator(runtime_paths, project)

    def fail(staging: Path) -> None:
        atomic_write_bytes(staging / "partial.json", b"evidence")
        raise OSError("caller artifact failed")

    with pytest.raises(OSError, match="artifact"):
        runtime.start_run("run_artifact_failure", initialize_artifacts=fail)

    assert not runtime_paths.run(project.project_id, "run_artifact_failure").exists()
    assert runtime_paths.initializing_run(project.project_id, "run_artifact_failure").is_dir()
    assert not runtime_paths.active_run(project.project_id).exists()
