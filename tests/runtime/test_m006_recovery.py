from __future__ import annotations

import pytest

from agentgraph.runtime import RecoveryAction
from agentgraph.write import WriteSliceRequest
from tests.integration.conftest import git
from tests.integration.test_m006_vertical_slice import _runner, _target


class InvocationFault:
    def __init__(self, target: int):
        self.target = target
        self.count = 0

    def __call__(self, stage: str) -> None:
        if stage == "after_node_invocation":
            self.count += 1
            if self.count == self.target:
                raise RuntimeError("injected M006 interruption")


def test_interrupted_implement_blocks_recovery_without_provider_rerun(tmp_path) -> None:
    target = _target(tmp_path)
    runner = _runner(target, tmp_path / "runtime")
    runner.fault = InvocationFault(8)

    with pytest.raises(RuntimeError, match="interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))

    assessment = runner.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.BLOCKED
    assert assessment.resume_node == "IMPLEMENT"
    workspace = runner.paths.run("prj_m006_fixture", "run_m006_fixture") / "workspace"
    assert workspace.is_dir()
    assert git(target, "status", "--porcelain") == b""


def test_interrupted_close_after_commit_blocks_without_duplicate_commit(tmp_path) -> None:
    target = _target(tmp_path)
    base = git(target, "rev-parse", "HEAD").strip()
    runner = _runner(target, tmp_path / "runtime")
    runner.fault = InvocationFault(11)

    with pytest.raises(RuntimeError, match="interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))

    branch_head = git(target, "rev-parse", "refs/heads/work/e001").strip()
    assert branch_head != base
    assert git(target, "rev-list", "--count", f"{base.decode()}..work/e001").strip() == b"1"
    assessment = runner.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.BLOCKED
    assert assessment.resume_node == "CLOSE_TASK"
    assert git(target, "rev-parse", "refs/heads/work/e001").strip() == branch_head
    assert git(target, "rev-list", "--count", f"{base.decode()}..work/e001").strip() == b"1"
