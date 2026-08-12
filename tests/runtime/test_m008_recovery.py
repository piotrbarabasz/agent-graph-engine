from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.runtime import RecoveryAction
from agentgraph.write import WriteSliceOutcome, WriteSliceRequest
from agentgraph.write.evidence import read_evidence
from tests.integration.test_m006_vertical_slice import _target
from tests.integration.test_m008_analysis import (
    CapturingChangeProvider,
    RecordingAgentProvider,
    runner,
)
from tests.runtime.test_m006_recovery import InvocationFault, TransitionFault


@pytest.mark.parametrize(
    ("transition_count", "resume_node"),
    (
        (5, "BUILD_TASK_PACKAGE"),
        (6, "ASSESS_RISK"),
        (7, "IMPLEMENT"),
    ),
)
def test_completed_analysis_nodes_are_not_reinvoked_after_restart(
    tmp_path, transition_count, resume_node
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RecordingAgentProvider()
    change = CapturingChangeProvider()
    first = runner(
        target,
        runtime,
        agent,
        change,
        fault=TransitionFault(transition_count),
    )

    with pytest.raises(RuntimeError, match="clean process stop"):
        first.run(WriteSliceRequest(scope_id="E001"))

    calls_at_restart = len(agent.requests)
    restarted = runner(target, runtime, agent, change)
    assessment = restarted.assess_recovery("run_m008_fixture")
    assert assessment.action is RecoveryAction.CLEAN_RESUME
    assert assessment.resume_node == resume_node
    report = restarted.resume("run_m008_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes[0] == resume_node
    assert len(agent.requests) == 3
    assert [request.operation_id for request, _ in agent.requests[:calls_at_restart]] == [
        "explore",
        "build_task_package",
        "assess_risk",
    ][:calls_at_restart]
    assert len(change.requests) == 1
    if resume_node == "IMPLEMENT":
        request = change.requests[0]
        assert "Preserve the tracked baseline." in request.effective_requirements
        assert "The new module remains deterministic." in request.effective_acceptance_criteria


@pytest.mark.parametrize(
    ("invocation_count", "node_id"),
    (
        (5, "EXPLORE"),
        (6, "BUILD_TASK_PACKAGE"),
        (7, "ASSESS_RISK"),
    ),
)
def test_interrupted_read_only_node_reruns_with_new_attempt_evidence(
    tmp_path, invocation_count, node_id
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RecordingAgentProvider()
    change = CapturingChangeProvider()
    first = runner(
        target,
        runtime,
        agent,
        change,
        fault=InvocationFault(invocation_count),
    )

    with pytest.raises(RuntimeError, match="interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))

    assessment = first.assess_recovery("run_m008_fixture")
    assert assessment.action is RecoveryAction.RERUN_INTERRUPTED_NODE
    assert assessment.resume_node == node_id
    report = runner(target, runtime, agent, change).resume("run_m008_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    operation = node_id.casefold()
    if operation == "build_task_package" or operation == "assess_risk":
        expected_operation = operation
    else:
        expected_operation = "explore"
    assert sum(request.operation_id == expected_operation for request, _ in agent.requests) == 2
    evidence_root = Path(report.runtime_path or "") / "provider" / "fake" / "agents" / node_id
    attempts = tuple(path for path in evidence_root.iterdir() if path.is_dir())
    assert len(attempts) == 2
    assert all((path / "analysis.json").is_file() for path in attempts)
    documents = tuple(read_evidence(path / "analysis.json") for path in attempts)
    canonical_ids = {document["node_attempt_id"] for document in documents}
    invocation_ids = {document["provider_invocation_id"] for document in documents}
    assert len(canonical_ids) == 1
    assert canonical_ids == {
        context.node_attempt_id
        for request, context in agent.requests
        if request.operation_id == expected_operation
    }
    assert len(invocation_ids) == 2


def test_recorded_explore_result_replay_does_not_call_provider_again(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RecordingAgentProvider()

    class RecordedResultFault:
        count = 0

        def __call__(self, stage):
            if stage == "after_node_result_recorded":
                self.count += 1
                if self.count == 5:
                    raise RuntimeError("recorded result stop")

    first = runner(target, runtime, agent, fault=RecordedResultFault())
    with pytest.raises(RuntimeError, match="recorded result stop"):
        first.run(WriteSliceRequest(scope_id="E001"))

    assert len(agent.requests) == 1
    assert (
        first.assess_recovery("run_m008_fixture").action is RecoveryAction.REAPPLY_RECORDED_RESULT
    )
    report = runner(target, runtime, agent).resume("run_m008_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert [request.operation_id for request, _ in agent.requests].count("explore") == 1


def test_implement_blocks_when_graph_bound_analysis_evidence_is_tampered(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    first = runner(
        target,
        runtime,
        RecordingAgentProvider(),
        fault=TransitionFault(7),
    )
    with pytest.raises(RuntimeError, match="clean process stop"):
        first.run(WriteSliceRequest(scope_id="E001"))

    run_path = first.paths.run("prj_m008_fixture", "run_m008_fixture")
    evidence = next((run_path / "provider" / "fake" / "agents" / "EXPLORE").glob("*/analysis.json"))
    raw = evidence.read_text(encoding="utf-8")
    evidence.write_text(
        raw.replace('"content_digest":"sha256:', '"content_digest":"sha256:0'),
        encoding="utf-8",
    )
    change = CapturingChangeProvider()
    report = runner(target, runtime, RecordingAgentProvider(), change).resume("run_m008_fixture")

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "agent_analysis_evidence_mismatch"
    assert change.requests == []
