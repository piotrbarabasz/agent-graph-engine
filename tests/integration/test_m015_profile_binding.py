from __future__ import annotations

from agentgraph.config import (
    AgentGraphConfig,
    AgentsConfig,
    CodexConfig,
    ExecutionProfile,
    PolicyConfig,
    PublishConfig,
    ReviewConfig,
    SpecKitConfig,
    WorkConfig,
)
from agentgraph.write import WriteSliceOutcome, WriteSliceRequest
from agentgraph.write.evidence import read_evidence

from .test_m006_vertical_slice import NewFileProvider, _runner, _target


def _profile(*, executable: str = "codex", repairs: int = 0) -> ExecutionProfile:
    config = AgentGraphConfig(
        1,
        WorkConfig("speckit", SpecKitConfig()),
        AgentsConfig("codex", CodexConfig()),
        ReviewConfig(False, False),
        PolicyConfig(repairs, 1, 3600, 120, 30, "per_work_item"),
        PublishConfig(False, "github", "origin", True),
    )
    return ExecutionProfile.create(config, executable)


def test_new_run_persists_profile_in_initialization(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    profile = _profile()

    report = _runner(target, runtime, NewFileProvider(), execution_profile=profile).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.run_id is not None
    path = runtime / "projects" / "prj_m006_fixture" / "runs" / report.run_id
    run_inputs = read_evidence(path / "run-inputs.json")["payload"]
    evidence = read_evidence(path / "execution-profile.json")
    assert run_inputs["execution_profile_digest"] == profile.digest
    assert evidence["execution_profile_digest"] == profile.digest
    assert evidence["payload"]["digest"] == profile.digest


def test_resume_rejects_profile_drift_before_provider_invocation(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    original = _profile()
    first = _runner(target, runtime, NewFileProvider(), execution_profile=original).run(
        WriteSliceRequest(scope_id="E001")
    )
    assert first.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED

    changed = ExecutionProfile.create(
        AgentGraphConfig(
            1,
            WorkConfig("speckit", SpecKitConfig()),
            AgentsConfig("codex", CodexConfig()),
            ReviewConfig(False, False),
            PolicyConfig(0, 1, 3600, 120, 30, "per_work_item"),
            PublishConfig(False, "github", "origin", True),
        ),
        "other",
    )
    resumed = _runner(target, runtime, NewFileProvider(), execution_profile=changed).resume(
        first.run_id or ""
    )

    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "execution_profile_mismatch"


def test_profile_snapshot_missing_fails_closed(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    profile = _profile()
    first = _runner(target, runtime, execution_profile=profile).run(
        WriteSliceRequest(scope_id="E001")
    )
    run_path = runtime / "projects" / "prj_m006_fixture" / "runs" / (first.run_id or "")
    (run_path / "execution-profile.json").unlink()

    resumed = _runner(target, runtime, execution_profile=profile).resume(first.run_id or "")

    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "execution_profile_mismatch"


def test_legacy_run_does_not_require_profile_evidence(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    first = _runner(target, runtime).run(WriteSliceRequest(scope_id="E001"))
    run_path = runtime / "projects" / "prj_m006_fixture" / "runs" / (first.run_id or "")

    assert not (run_path / "execution-profile.json").exists()
    resumed = _runner(target, runtime).resume(first.run_id or "")
    assert resumed.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
