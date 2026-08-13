from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentgraph.core import CheckpointOutcome, RiskLevel, RunStatus
from agentgraph.runtime import CheckpointRequestRecord, RecoveryAction, StateStore
from agentgraph.runtime.codec import decode_value, parse_json_bytes, sha256_digest
from agentgraph.write import CheckpointError, WriteSliceOutcome, WriteSliceRequest
from agentgraph.write.models import WriteInputs
from tests.integration.conftest import git
from tests.integration.test_m006_vertical_slice import _target
from tests.integration.test_m008_analysis import (
    CapturingChangeProvider,
    RecordingAgentProvider,
    runner,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _paused(tmp_path, *, risk="critical", requested=False, clock=None):
    target = _target(tmp_path)
    change = CapturingChangeProvider()
    instance = runner(
        target,
        tmp_path / "runtime",
        RecordingAgentProvider(risk=risk, checkpoint=requested),
        change,
        clock=clock or Clock(),
        checkpoint_nonce_factory=lambda: "m011-secure-test-nonce",
    )
    report = instance.run(WriteSliceRequest(scope_id="E001"))
    assert report.checkpoint is not None
    return target, instance, change, report


def test_critical_run_pauses_durably_before_checkpoint_node_or_write(tmp_path) -> None:
    target, _, changes, report = _paused(tmp_path)

    assert report.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert report.graph_state.run.status is RunStatus.RUNNING
    assert report.graph_state.graph.current_node == "HUMAN_CHECKPOINT"
    assert report.graph_state.graph.pending_resume_node == "IMPLEMENT"
    assert "HUMAN_CHECKPOINT" not in report.executed_nodes
    assert changes.requests == []
    assert report.workspace_path is None
    run_path = Path(report.runtime_path)
    assert not (run_path / "final.json").exists()
    assert (run_path.parents[1] / "active-run.json").is_file()
    assert (run_path / "checkpoints" / report.checkpoint.checkpoint_id / "request.json").is_file()
    assert not any(path.name == "checkpoints" for path in target.rglob("checkpoints"))
    assert git(target, "status", "--porcelain") == b""
    assert git(target, "branch", "--show-current").strip() == b"main"


def test_request_binds_exact_state_inputs_tree_capability_and_operations(tmp_path) -> None:
    target, _, _, report = _paused(tmp_path)
    request_path = (
        Path(report.runtime_path) / "checkpoints" / report.checkpoint.checkpoint_id / "request.json"
    )
    request = decode_value(parse_json_bytes(request_path.read_bytes()), CheckpointRequestRecord)
    inputs_document = json.loads(
        (Path(report.runtime_path) / "write-inputs.json").read_text(encoding="utf-8")
    )
    inputs = decode_value(inputs_document["payload"], WriteInputs)
    state = report.graph_state

    assert request.state_version == report.graph_state.state_version
    assert request.state_digest == StateStore.digest_for_state(state)
    assert request.source_revision == report.source_revision
    assert request.baseline_head == report.baseline_head
    assert request.risk_level is RiskLevel.CRITICAL
    assert request.package_digest == sha256_digest(inputs.package)
    assert request.write_inputs_digest == sha256_digest(inputs)
    assert request.capability_fingerprint == inputs.capability_fingerprint
    assert request.operations_digest == sha256_digest(
        {
            "changes": state.changes,
            "validation": state.validation,
            "review": state.review,
            "repair": state.repair,
            "commits": state.commits,
            "push": state.push,
            "pull_request": state.pull_request,
        }
    )
    assert request.baseline_tree_id == git(target, "rev-parse", "HEAD^{tree}").decode().strip()


def test_resume_reuses_same_request_nonce_and_expiry(tmp_path) -> None:
    _, instance, changes, first = _paused(tmp_path)

    second = instance.resume(first.run_id)
    third = instance.resume(first.run_id)

    assert second.outcome is third.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert second.checkpoint == first.checkpoint == third.checkpoint
    assert changes.requests == []


def test_resume_uses_persisted_ttl_not_new_runner_configuration(tmp_path) -> None:
    clock = Clock()
    target, _, _, first = _paused(tmp_path, clock=clock)
    replacement = runner(
        target,
        tmp_path / "runtime",
        RecordingAgentProvider(risk="critical"),
        clock=clock,
        checkpoint_ttl_seconds=86400,
    )

    resumed = replacement.resume(first.run_id)

    assert resumed.checkpoint == first.checkpoint
    assert resumed.checkpoint.expires_at == "2030-01-01T01:00:00Z"


def test_approved_decision_resumes_full_unchanged_write_pipeline(tmp_path) -> None:
    _, instance, changes, paused = _paused(tmp_path)
    checkpoint = paused.checkpoint

    decision = instance.submit_checkpoint(
        paused.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        nonce=checkpoint.nonce,
        outcome=CheckpointOutcome.APPROVED,
        actor="test-operator",
    )
    assert decision.actor == "test-operator"
    assert changes.requests == []

    completed = instance.resume(paused.run_id)

    assert completed.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert completed.commit_sha is not None
    assert completed.graph_state.graph.pending_resume_node is None
    assert completed.executed_nodes[:2] == ("HUMAN_CHECKPOINT", "IMPLEMENT")
    assert len(changes.requests) == 1


@pytest.mark.parametrize(
    ("outcome", "status"),
    (
        (CheckpointOutcome.REJECTED, RunStatus.BLOCKED),
        (CheckpointOutcome.CANCELLED, RunStatus.CANCELLED),
    ),
)
def test_non_approval_finalizes_without_implementation(tmp_path, outcome, status) -> None:
    _, instance, changes, paused = _paused(tmp_path)
    checkpoint = paused.checkpoint
    instance.submit_checkpoint(
        paused.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        nonce=checkpoint.nonce,
        outcome=outcome,
        actor="test-operator",
    )

    final = instance.resume(paused.run_id)

    assert final.graph_state.run.status is status
    assert final.graph_state.graph.current_node == "END"
    assert final.graph_state.graph.pending_resume_node is None
    assert changes.requests == []


def test_wrong_nonce_and_double_decision_are_rejected_immutably(tmp_path) -> None:
    _, instance, _, paused = _paused(tmp_path)
    checkpoint = paused.checkpoint
    with pytest.raises(CheckpointError, match="checkpoint_not_pending"):
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id="checkpoint-999-implement",
            nonce=checkpoint.nonce,
            outcome=CheckpointOutcome.APPROVED,
            actor="operator",
        )
    with pytest.raises(CheckpointError, match="checkpoint_nonce_mismatch"):
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            nonce="wrong",
            outcome=CheckpointOutcome.APPROVED,
            actor="operator",
        )
    decision_path = (
        Path(paused.runtime_path) / "checkpoints" / checkpoint.checkpoint_id / "decision.json"
    )
    assert not decision_path.exists()
    original = instance.submit_checkpoint(
        paused.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        nonce=checkpoint.nonce,
        outcome=CheckpointOutcome.APPROVED,
        actor="operator",
    )
    with pytest.raises(CheckpointError, match="checkpoint_already_decided"):
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            nonce=checkpoint.nonce,
            outcome=CheckpointOutcome.REJECTED,
            actor="second",
        )
    assert json.loads(decision_path.read_text(encoding="utf-8"))["decision_digest"] == (
        original.decision_digest
    )


@pytest.mark.parametrize("actor", ("", "bad\x00actor", "x" * 257))
def test_actor_is_explicit_and_strictly_validated(tmp_path, actor) -> None:
    _, instance, _, paused = _paused(tmp_path)
    with pytest.raises(CheckpointError, match="checkpoint_actor_invalid"):
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id=paused.checkpoint.checkpoint_id,
            nonce=paused.checkpoint.nonce,
            outcome=CheckpointOutcome.APPROVED,
            actor=actor,
        )


def test_expired_submission_is_rejected_and_expired_resume_blocks(tmp_path) -> None:
    clock = Clock()
    _, instance, changes, paused = _paused(tmp_path, clock=clock)
    clock.advance(3601)
    with pytest.raises(CheckpointError, match="checkpoint_expired"):
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id=paused.checkpoint.checkpoint_id,
            nonce=paused.checkpoint.nonce,
            outcome=CheckpointOutcome.APPROVED,
            actor="operator",
        )

    final = instance.resume(paused.run_id)
    assert final.outcome is WriteSliceOutcome.BLOCKED
    assert final.issues[0].code == "checkpoint_expired"
    assert changes.requests == []


def test_decision_before_expiry_remains_valid_after_expiry(tmp_path) -> None:
    clock = Clock()
    _, instance, changes, paused = _paused(tmp_path, clock=clock)
    instance.submit_checkpoint(
        paused.run_id,
        checkpoint_id=paused.checkpoint.checkpoint_id,
        nonce=paused.checkpoint.nonce,
        outcome=CheckpointOutcome.APPROVED,
        actor="operator",
    )
    clock.advance(7200)

    report = instance.resume(paused.run_id)
    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert len(changes.requests) == 1


def test_target_drift_after_request_prevents_decision_and_implementation(tmp_path) -> None:
    target, instance, changes, paused = _paused(tmp_path)
    (target / "drift.txt").write_text("drift\n", encoding="utf-8")

    with pytest.raises(CheckpointError, match="checkpoint_binding_mismatch"):
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id=paused.checkpoint.checkpoint_id,
            nonce=paused.checkpoint.nonce,
            outcome=CheckpointOutcome.APPROVED,
            actor="operator",
        )
    resumed = instance.resume(paused.run_id)
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "checkpoint_binding_mismatch"
    assert changes.requests == []


def test_source_drift_while_waiting_fails_closed(tmp_path) -> None:
    target, instance, changes, paused = _paused(tmp_path)
    source = target / "specs" / "one" / "tasks.md"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(CheckpointError, match="checkpoint_binding_mismatch"):
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id=paused.checkpoint.checkpoint_id,
            nonce=paused.checkpoint.nonce,
            outcome=CheckpointOutcome.APPROVED,
            actor="operator",
        )
    resumed = instance.resume(paused.run_id)
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "checkpoint_binding_mismatch"
    assert changes.requests == []


@pytest.mark.parametrize("drift", ("target", "source"))
def test_drift_after_approval_never_reaches_implement(tmp_path, drift) -> None:
    target, instance, changes, paused = _paused(tmp_path)
    instance.submit_checkpoint(
        paused.run_id,
        checkpoint_id=paused.checkpoint.checkpoint_id,
        nonce=paused.checkpoint.nonce,
        outcome=CheckpointOutcome.APPROVED,
        actor="operator",
    )
    path = target / ("drift.txt" if drift == "target" else "specs/one/tasks.md")
    path.write_text(
        "drift\n" if drift == "target" else path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    resumed = instance.resume(paused.run_id)
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "checkpoint_binding_mismatch"
    assert changes.requests == []


@pytest.mark.parametrize("evidence_name", ("request.json", "decision.json"))
def test_checkpoint_evidence_tamper_fails_closed(tmp_path, evidence_name) -> None:
    _, instance, changes, paused = _paused(tmp_path)
    if evidence_name == "decision.json":
        instance.submit_checkpoint(
            paused.run_id,
            checkpoint_id=paused.checkpoint.checkpoint_id,
            nonce=paused.checkpoint.nonce,
            outcome=CheckpointOutcome.APPROVED,
            actor="operator",
        )
    path = (
        Path(paused.runtime_path) / "checkpoints" / paused.checkpoint.checkpoint_id / evidence_name
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    digest_field = "request_digest" if evidence_name == "request.json" else "decision_digest"
    document[digest_field] = "sha256:" + "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    resumed = instance.resume(paused.run_id)
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "checkpoint_evidence_invalid"
    assert changes.requests == []


def test_recomputed_request_with_corrupt_baseline_tree_still_mismatches(tmp_path) -> None:
    _, instance, changes, paused = _paused(tmp_path)
    path = (
        Path(paused.runtime_path) / "checkpoints" / paused.checkpoint.checkpoint_id / "request.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["baseline_tree_id"] = "0" * 40
    document["request_digest"] = sha256_digest(
        {key: value for key, value in document.items() if key != "request_digest"}
    )
    path.write_text(json.dumps(document), encoding="utf-8")

    resumed = instance.resume(paused.run_id)
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "checkpoint_binding_mismatch"
    assert changes.requests == []


def test_source_critical_and_explicit_agent_request_both_route_to_checkpoint(tmp_path) -> None:
    target = _target(tmp_path)
    tasks = target / "specs" / "one" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace("Risk: medium", "Risk: critical"),
        encoding="utf-8",
    )
    git(target, "add", "--", "specs/one/tasks.md")
    git(target, "commit", "--quiet", "-m", "critical source")
    source_report = runner(
        target,
        tmp_path / "runtime-source",
        RecordingAgentProvider(risk="low"),
    ).run(WriteSliceRequest(scope_id="E001"))
    assert source_report.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert source_report.graph_state.risk.level is RiskLevel.CRITICAL

    second = tmp_path / "second"
    second.mkdir()
    target_two = _target(second)
    requested_report = runner(
        target_two,
        tmp_path / "runtime-requested",
        RecordingAgentProvider(risk="high", checkpoint=True),
    ).run(WriteSliceRequest(scope_id="E001"))
    assert requested_report.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert requested_report.graph_state.risk.level is RiskLevel.CRITICAL


def test_high_risk_without_request_remains_non_checkpoint_flow(tmp_path) -> None:
    target = _target(tmp_path)
    report = runner(
        target,
        tmp_path / "runtime",
        RecordingAgentProvider(risk="high", checkpoint=False),
    ).run(WriteSliceRequest(scope_id="E001"))
    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.checkpoint is None


def test_nonce_and_actor_never_enter_agent_prompts(tmp_path) -> None:
    target = _target(tmp_path)
    agent = RecordingAgentProvider(risk="critical")
    instance = runner(
        target,
        tmp_path / "runtime",
        agent,
        checkpoint_nonce_factory=lambda: "unique-secret-nonce",
    )
    paused = instance.run(WriteSliceRequest(scope_id="E001"))
    instance.submit_checkpoint(
        paused.run_id,
        checkpoint_id=paused.checkpoint.checkpoint_id,
        nonce=paused.checkpoint.nonce,
        outcome=CheckpointOutcome.REJECTED,
        actor="unique-human-actor",
    )
    instance.resume(paused.run_id)

    prompts = "\n".join(request.prompt for request, _ in agent.requests)
    assert "unique-secret-nonce" not in prompts
    assert "unique-human-actor" not in prompts


def test_crash_after_checkpoint_transition_recreates_missing_request(tmp_path) -> None:
    transitions = 0

    def fault(stage):
        nonlocal transitions
        if stage == "after_transition_committed":
            transitions += 1
            if transitions == 7:
                raise RuntimeError("crash before checkpoint request")

    target = _target(tmp_path)
    instance = runner(
        target,
        tmp_path / "runtime",
        RecordingAgentProvider(risk="critical"),
        fault=fault,
    )
    with pytest.raises(RuntimeError, match="crash before checkpoint request"):
        instance.run(WriteSliceRequest(scope_id="E001"))

    resumed = runner(
        target,
        tmp_path / "runtime",
        RecordingAgentProvider(risk="critical"),
    ).resume("run_m008_fixture")
    assert resumed.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert resumed.checkpoint is not None


def test_crash_after_request_write_reuses_complete_request(tmp_path) -> None:
    armed = True

    def fault(stage):
        if armed and stage == "CHECKPOINT_REQUEST_PERSISTED":
            raise RuntimeError("crash after request")

    target = _target(tmp_path)
    instance = runner(
        target,
        tmp_path / "runtime",
        RecordingAgentProvider(risk="critical"),
        fault=fault,
        checkpoint_nonce_factory=lambda: "stable-crash-nonce",
    )
    with pytest.raises(RuntimeError, match="crash after request"):
        instance.run(WriteSliceRequest(scope_id="E001"))
    armed = False

    resumed = instance.resume("run_m008_fixture")
    assert resumed.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert resumed.checkpoint.nonce == "stable-crash-nonce"


@pytest.mark.parametrize(
    ("stage", "expected"),
    (
        ("after_node_started", RecoveryAction.RERUN_INTERRUPTED_NODE),
        ("after_node_result_recorded", RecoveryAction.REAPPLY_RECORDED_RESULT),
        ("after_state_cas", RecoveryAction.COMPLETE_TRANSITION_MARKER),
    ),
)
def test_interrupted_checkpoint_recovery_reuses_one_decision(tmp_path, stage, expected) -> None:
    armed = False

    def fault(current):
        if armed and current == stage:
            raise RuntimeError("checkpoint crash")

    _, instance, changes, paused = _paused(tmp_path)
    checkpoint = paused.checkpoint
    instance.fault = fault
    instance.submit_checkpoint(
        paused.run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        nonce=checkpoint.nonce,
        outcome=CheckpointOutcome.REJECTED,
        actor="operator",
    )
    armed = True
    with pytest.raises(RuntimeError, match="checkpoint crash"):
        instance.resume(paused.run_id)
    armed = False

    assert instance.assess_recovery(paused.run_id).action is expected
    final = instance.resume(paused.run_id)
    assert final.graph_state.run.status is RunStatus.BLOCKED
    assert changes.requests == []
