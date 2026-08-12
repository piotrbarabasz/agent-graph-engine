from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from agentgraph.core import RunStatus
from agentgraph.infra import GitAdapter
from agentgraph.providers.codex import CodexChangeProvider, CodexProviderConfig
from agentgraph.runtime import RecoveryAction
from agentgraph.write import WriteSliceOutcome, WriteSliceRequest
from tests.integration.conftest import git, semantic_git_state, working_tree_bytes
from tests.integration.test_m006_vertical_slice import _runner, _target
from tests.runtime.test_m006_recovery import InvocationFault, TransitionFault


def _provider(timeout: float = 5.0) -> CodexChangeProvider:
    script = Path(__file__).with_name("_fake_codex.py")
    config = CodexProviderConfig(
        executable=sys.executable,
        executable_arguments=(str(script),),
        timeout_seconds=timeout,
    )
    return CodexChangeProvider(
        config=config,
        git_adapter=GitAdapter(executable=shutil.which("git") or "git"),
    )


def _fake_evidence(tmp_path: Path, monkeypatch) -> Path:
    count = tmp_path / "codex-count.txt"
    monkeypatch.setenv("FAKE_CODEX_COUNT", str(count))
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(tmp_path / "codex-capture.json"))
    monkeypatch.setenv(
        "FAKE_CODEX_RESULT",
        json.dumps(
            {
                "schema_version": 1,
                "status": "changes",
                "changes": [{"path": "src/t001.py", "content": "value = 42\n"}],
                "reason_code": None,
                "message": None,
            }
        ),
    )
    return count


def test_fake_codex_full_write_slice_creates_one_verified_commit(tmp_path, monkeypatch) -> None:
    target = _target(tmp_path)
    before_git = semantic_git_state(target)
    before_bytes = working_tree_bytes(target)
    source_before = (target / "specs" / "one" / "tasks.md").read_bytes()
    count = _fake_evidence(tmp_path, monkeypatch)

    report = _runner(target, tmp_path / "runtime", _provider()).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.changed_paths == ("src/t001.py",)
    assert report.commit_sha is not None
    assert count.read_text(encoding="ascii") == "1"
    assert git(target, "show", f"{report.commit_sha}:src/t001.py") == b"value = 42\n"
    assert semantic_git_state(target) == before_git
    assert working_tree_bytes(target) == before_bytes
    assert (target / "specs" / "one" / "tasks.md").read_bytes() == source_before
    codex_dir = Path(report.runtime_path or "") / "provider" / "codex"
    assert {path.name for path in codex_dir.iterdir()} == {
        "schema.json",
        "final-result.json",
        "codex-receipt.json",
        "codex-proposal.json",
    }


def test_blocked_codex_proposal_finishes_run_blocked_without_commit(tmp_path, monkeypatch) -> None:
    target = _target(tmp_path)
    base = git(target, "rev-parse", "HEAD").strip().decode()
    _fake_evidence(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "FAKE_CODEX_RESULT",
        json.dumps(
            {
                "schema_version": 1,
                "status": "blocked",
                "changes": [],
                "reason_code": "requires_delete",
                "message": "Task requires deleting a file.",
            }
        ),
    )

    report = _runner(target, tmp_path / "runtime", _provider()).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.BLOCKED
    assert report.issues[0].code == "requires_delete"
    assert git(target, "rev-list", "--count", f"{base}..work/e001").strip() == b"0"


@pytest.mark.parametrize("mode", ("tracked", "untracked", "staged", "head"))
def test_provider_repository_mutation_stops_before_engine_apply(
    tmp_path, monkeypatch, mode
) -> None:
    target = _target(tmp_path)
    before_git = semantic_git_state(target)
    before_bytes = working_tree_bytes(target)
    base = git(target, "rev-parse", "HEAD").strip().decode()
    _fake_evidence(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)

    report = _runner(target, tmp_path / "runtime", _provider()).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "provider_mutated_repository"
    assert report.changeset_digest is None
    assert not (Path(report.runtime_path or "") / "operations" / "implement-applied.json").exists()
    assert semantic_git_state(target) == before_git
    assert working_tree_bytes(target) == before_bytes
    assert git(target, "rev-list", "--count", f"{base}..work/e001").strip() in {b"0", b"1"}


def test_interrupted_codex_implement_never_invokes_provider_on_restart(
    tmp_path, monkeypatch
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    count = _fake_evidence(tmp_path, monkeypatch)
    first = _runner(target, runtime, _provider(), fault=InvocationFault(8))
    with pytest.raises(RuntimeError, match="interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))

    assert count.read_text(encoding="ascii") == "1"
    assessment = first.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.BLOCKED
    assert assessment.resume_node == "IMPLEMENT"
    report = _runner(target, runtime, _provider()).resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert count.read_text(encoding="ascii") == "1"


def test_committed_implement_transition_resumes_without_second_codex_call(
    tmp_path, monkeypatch
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    count = _fake_evidence(tmp_path, monkeypatch)
    first = _runner(target, runtime, _provider(), fault=TransitionFault(8))
    with pytest.raises(RuntimeError, match="clean process stop"):
        first.run(WriteSliceRequest(scope_id="E001"))

    assert count.read_text(encoding="ascii") == "1"
    restarted = _runner(target, runtime, _provider())
    assessment = restarted.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.CLEAN_RESUME
    assert assessment.resume_node == "VALIDATE"
    report = restarted.resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert count.read_text(encoding="ascii") == "1"
