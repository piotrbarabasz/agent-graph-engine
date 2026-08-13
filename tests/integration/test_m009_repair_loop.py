from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.agents import AgentResponse
from agentgraph.core import RepairClassification, RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RecoveryAction, RuntimePaths
from agentgraph.runtime.codec import sha256_digest
from agentgraph.write import (
    ChangeIntent,
    ChangeSet,
    FileChange,
    RepairPolicyError,
    RepairValidationDiagnosticKind,
    WriteSliceOutcome,
    WriteSliceRequest,
    WriteSliceRunner,
)
from agentgraph.write.evidence import read_evidence
from tests.integration.conftest import git, semantic_git_state, working_tree_bytes
from tests.integration.test_m006_vertical_slice import _target


class RepairAgent:
    evidence_namespace = "m009-fake"

    def __init__(self, routes=("debugger",), mutate_classifier=None, *, blocked_classifier=False):
        self.routes = iter(routes)
        self.mutate_classifier = mutate_classifier
        self.blocked_classifier = blocked_classifier
        self.calls: list[str] = []
        self.requests = []
        self.classifier_contexts = []

    def invoke(self, request, context):
        self.calls.append(request.operation_id)
        self.requests.append(request)
        if request.operation_id == "explore":
            payload = {
                "schema_version": 1,
                "status": "success",
                "relevant_files": ["tracked.txt"],
                "architecture_observations": ["A small deterministic module."],
                "derived_requirements": [],
                "derived_acceptance_criteria": [],
                "derived_constraints": [],
                "architecture_invariants": [],
                "uncertainties": [],
                "reason_code": None,
                "message": None,
            }
        elif request.operation_id == "build_task_package":
            payload = {
                "schema_version": 1,
                "status": "success",
                "objective": "Implement the declared behavior.",
                "implementation_steps": ["Correct src/t001.py."],
                "recommended_change_paths": ["src/t001.py"],
                "supporting_read_paths": [],
                "validation_focus": ["Use canonical validation."],
                "assumptions": [],
                "unresolved_questions": [],
                "reason_code": None,
                "message": None,
            }
        elif request.operation_id == "assess_risk":
            payload = {
                "schema_version": 1,
                "status": "success",
                "risk_level": "medium",
                "reasons": ["Bounded repair fixture."],
                "sensitive_areas": [],
                "destructive_change_concerns": [],
                "requests_human_checkpoint": False,
                "reason_code": None,
                "message": None,
            }
        else:
            assert request.operation_id == "classify_failure"
            self.classifier_contexts.append(context)
            if self.mutate_classifier is not None:
                self.mutate_classifier(context)
            if self.blocked_classifier:
                payload = {
                    "schema_version": 1,
                    "status": "blocked",
                    "classification": None,
                    "rationale": None,
                    "signals": [],
                    "reason_code": "classification_evidence_insufficient",
                    "message": "The bounded evidence cannot support a safe repair route.",
                }
            else:
                payload = {
                    "schema_version": 1,
                    "status": "success",
                    "classification": next(self.routes),
                    "rationale": "The validation receipt identifies a logic defect.",
                    "signals": ["validation_failed"],
                    "reason_code": None,
                    "message": None,
                }
        return AgentResponse(
            payload,
            "m009-fake-agent",
            "1",
            None,
            request.input_digest,
            sha256_digest(payload),
            "response.json",
        )


class SequentialChangeProvider:
    def __init__(self, values=(0, 2), *, mutate_on_repair=False):
        self.values = iter(values)
        self.mutate_on_repair = mutate_on_repair
        self.requests = []
        self.contexts = []
        self.changesets = []

    def propose(self, request, context):
        self.requests.append(request)
        self.contexts.append(context)
        candidate = context.repository_root / "src" / "t001.py"
        if request.intent is not ChangeIntent.IMPLEMENT and self.mutate_on_repair:
            candidate.write_text("value = 99\n", encoding="utf-8")
        before = hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None
        value = next(self.values)
        result = ChangeSet.create((FileChange("src/t001.py", before, f"value = {value}\n"),))
        self.changesets.append(result)
        return result


class ShrinkingChangeProvider:
    def __init__(self):
        self.calls = 0

    def propose(self, request, context):
        self.calls += 1
        source = context.repository_root / "src" / "t001.py"
        test = context.repository_root / "tests" / "t001.py"
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        test_hash = hashlib.sha256(test.read_bytes()).hexdigest()
        if request.intent is ChangeIntent.IMPLEMENT:
            return ChangeSet.create(
                (
                    FileChange("src/t001.py", source_hash, "value = 0\n"),
                    FileChange("tests/t001.py", test_hash, "temporary = True\n"),
                )
            )
        return ChangeSet.create(
            (
                FileChange("src/t001.py", source_hash, "value = 2\n"),
                FileChange("tests/t001.py", test_hash, "baseline = True\n"),
            )
        )


class WhitespaceRepairProvider:
    def __init__(self):
        self.requests = []

    def propose(self, request, context):
        self.requests.append(request)
        candidate = context.repository_root / "src" / "t001.py"
        before = hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None
        content = "value = 0  \n" if request.intent is ChangeIntent.IMPLEMENT else "value = 2\n"
        return ChangeSet.create((FileChange("src/t001.py", before, content),))


class StageFault:
    def __init__(self, stage: str, target: int):
        self.stage = stage
        self.target = target
        self.count = 0

    def __call__(self, stage: str):
        if stage == self.stage:
            self.count += 1
            if self.count == self.target:
                raise RuntimeError("injected M009 interruption")


def _runner(target, runtime, agent, changes, *, max_repair_cycles, fault=None):
    paths = RuntimePaths.resolve(runtime)
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        changes,
        agent_provider=agent,
        git_adapter=adapter,
        project_registry=ProjectRegistry(paths, project_id_factory=lambda: "prj_m009_fixture"),
        commit_identity=GitCommitIdentity("M009 Test", "m009@example.test"),
        run_id_factory=lambda: "run_m009_fixture",
        max_repair_cycles=max_repair_cycles,
        fault=fault,
    )


def _repair_target(tmp_path: Path) -> Path:
    command = (
        'python -c "from pathlib import Path; '
        "raise SystemExit(0 if 'value = 2' in Path('src/t001.py').read_text() else 7)\""
    )
    return _target(tmp_path, command)


def test_debugger_repair_is_graph_driven_cycle_aware_and_commits_once(tmp_path) -> None:
    target = _repair_target(tmp_path)
    before_git = semantic_git_state(target)
    before_bytes = working_tree_bytes(target)
    agent = RepairAgent()
    changes = SequentialChangeProvider()

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=2).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.PAUSED
    assert report.graph_state.repair.count == 1
    assert report.graph_state.repair.history[0].classification is RepairClassification.DEBUGGER
    assert report.graph_state.failure.category is None
    assert report.graph_state.failure.code is None
    assert report.executed_nodes == (
        "START",
        "DISCOVER_PROJECT",
        "PREFLIGHT",
        "SELECT_WORK",
        "EXPLORE",
        "BUILD_TASK_PACKAGE",
        "ASSESS_RISK",
        "IMPLEMENT",
        "VALIDATE",
        "CLASSIFY_FAILURE",
        "DEBUGGER",
        "VALIDATE",
        "REVIEW",
        "CLOSE_TASK",
        "MORE_WORK",
        "FINALIZE",
    )
    assert agent.calls.count("classify_failure") == 1
    assert [request.intent for request in changes.requests] == [
        ChangeIntent.IMPLEMENT,
        ChangeIntent.DEBUGGER,
    ]
    assert changes.contexts[1].repair_cycle == 1
    assert agent.classifier_contexts[0].repository_root.name == "workspace"
    operations = Path(report.runtime_path or "") / "operations"
    assert (operations / "workspace-manifest.json").is_file()
    for name in (
        "context.json",
        "proposal.json",
        "applied.json",
        "workspace-manifest.json",
        "validation.json",
        "review.json",
    ):
        assert (operations / "repairs" / "001" / name).is_file()
    assert semantic_git_state(target) == before_git
    assert working_tree_bytes(target) == before_bytes
    assert git(target, "rev-list", "--count", f"{report.base_head}..work/e001").strip() == b"1"


def test_two_repair_cycles_are_distinct_and_bounded(tmp_path) -> None:
    target = _repair_target(tmp_path)
    agent = RepairAgent(("programmer", "debugger"))
    changes = SequentialChangeProvider((0, 1, 2))

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=2).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state is not None
    assert report.graph_state.repair.count == 2
    assert [item.id for item in report.graph_state.repair.history] == [
        "repair-001",
        "repair-002",
    ]
    assert agent.calls.count("classify_failure") == 2
    assert len(changes.requests) == 3
    assert report.executed_nodes.count("VALIDATE") == 3
    assert report.executed_nodes.count("CLASSIFY_FAILURE") == 2
    operations = Path(report.runtime_path or "") / "operations" / "repairs"
    assert (operations / "001" / "validation.json").is_file()
    assert (operations / "002" / "validation.json").is_file()
    for cycle, changeset in enumerate(changes.changesets[1:], start=1):
        directory = operations / f"{cycle:03d}"
        for name in ("proposal.json", "applied.json", "workspace-manifest.json"):
            evidence = read_evidence(directory / name)
            assert evidence["changeset_digest"] == changeset.digest
            assert evidence["repair_cycle"] == cycle
            assert evidence["changeset_digest"] != changes.changesets[cycle - 1].digest


def test_git_diff_check_failure_is_exposed_to_classifier_and_repair(tmp_path) -> None:
    target = _target(tmp_path, "python -c \"print('declared pass')\"")
    (target / "src").mkdir()
    (target / "src" / "t001.py").write_text("value = -1\n", encoding="utf-8")
    git(target, "add", "src/t001.py")
    git(target, "commit", "--quiet", "-m", "tracked diff-check fixture")
    agent = RepairAgent()
    changes = WhitespaceRepairProvider()

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=1).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes.count("VALIDATE") == 2
    assert agent.calls.count("classify_failure") == 1
    classifier_request = next(
        request for request in agent.requests if request.operation_id == "classify_failure"
    )
    assert "git_diff_check_worktree" in classifier_request.prompt
    repair_request = changes.requests[1]
    assert any(
        "kind=git_diff_check_worktree" in diagnostic and "status=FAILED" in diagnostic
        for diagnostic in repair_request.validation_diagnostics
    )
    context_payload = read_evidence(
        Path(report.runtime_path or "") / "operations" / "repairs" / "001" / "context.json"
    )["payload"]
    kinds = [item["kind"] for item in context_payload["validation_diagnostics"]]
    assert kinds == [
        RepairValidationDiagnosticKind.DECLARED_VALIDATION.value,
        RepairValidationDiagnosticKind.DECLARED_VALIDATION.value,
        RepairValidationDiagnosticKind.GIT_DIFF_CHECK_WORKTREE.value,
        RepairValidationDiagnosticKind.GIT_DIFF_CHECK_STAGED.value,
    ]
    failed = [
        item
        for item in context_payload["validation_diagnostics"]
        if item["kind"] == RepairValidationDiagnosticKind.GIT_DIFF_CHECK_WORKTREE.value
    ]
    assert failed[0]["status"] == "FAILED"
    assert failed[0]["stderr_preview"] or failed[0]["stdout_preview"]


def test_limit_exhaustion_skips_classifier_and_repair_provider(tmp_path) -> None:
    target = _repair_target(tmp_path)
    agent = RepairAgent(("debugger",))
    changes = SequentialChangeProvider((0, 1))

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=1).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.graph_state is not None
    assert report.graph_state.repair.count == 1
    assert len(report.graph_state.repair.history) == 1
    assert agent.calls.count("classify_failure") == 1
    assert len(changes.requests) == 2
    assert report.executed_nodes[-2:] == ("CLASSIFY_FAILURE", "FINALIZE")


def test_zero_repair_policy_preserves_pre_m009_failure_path(tmp_path) -> None:
    target = _repair_target(tmp_path)
    agent = RepairAgent()
    changes = SequentialChangeProvider((0,))

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=0).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.executed_nodes[-2:] == ("CLASSIFY_FAILURE", "FINALIZE")
    assert agent.calls.count("classify_failure") == 0
    assert len(changes.requests) == 1
    assert changes.requests[0].intent is ChangeIntent.IMPLEMENT


def test_repair_provider_same_dirty_path_mutation_is_detected(tmp_path) -> None:
    target = _repair_target(tmp_path)
    agent = RepairAgent()
    changes = SequentialChangeProvider(mutate_on_repair=True)

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=1).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "provider_mutated_repository"
    assert len(changes.requests) == 2
    assert "value = 99" in (Path(report.workspace_path or "") / "src" / "t001.py").read_text()


def test_classifier_same_dirty_path_mutation_is_detected_before_repair(tmp_path) -> None:
    target = _repair_target(tmp_path)

    def mutate(context):
        (context.repository_root / "src" / "t001.py").write_text("value = 77\n", encoding="utf-8")

    agent = RepairAgent(mutate_classifier=mutate)
    changes = SequentialChangeProvider()
    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=1).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "agent_provider_mutated_repository"
    assert len(changes.requests) == 1
    assert agent.calls.count("classify_failure") == 1


def test_validation_mutation_cannot_be_laundered_into_repair(tmp_path) -> None:
    command = (
        'python -c "from pathlib import Path; '
        "Path('src/t001.py').write_text('value = 88\\n'); raise SystemExit(7)\""
    )
    target = _target(tmp_path, command)
    agent = RepairAgent()
    changes = SequentialChangeProvider()

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=1).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "repair_workspace_lineage_mismatch"
    assert agent.calls.count("classify_failure") == 0
    assert len(changes.requests) == 1


def test_classifier_blocked_stops_before_repair(tmp_path) -> None:
    target = _repair_target(tmp_path)
    agent = RepairAgent(blocked_classifier=True)
    changes = SequentialChangeProvider()

    report = _runner(target, tmp_path / "runtime", agent, changes, max_repair_cycles=1).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "classification_evidence_insufficient"
    assert len(changes.requests) == 1


def test_final_manifest_can_shrink_to_current_effective_diff(tmp_path) -> None:
    target = _repair_target(tmp_path)
    (target / "src").mkdir()
    (target / "tests").mkdir()
    (target / "src" / "t001.py").write_text("value = -1\n", encoding="utf-8")
    (target / "tests" / "t001.py").write_text("baseline = True\n", encoding="utf-8")
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "manifest shrink baseline")

    report = _runner(
        target,
        tmp_path / "runtime",
        RepairAgent(),
        ShrinkingChangeProvider(),
        max_repair_cycles=1,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.changed_paths == ("src/t001.py",)
    assert report.graph_state is not None
    assert report.graph_state.changes.agent_reported_files == ("src/t001.py",)
    assert git(
        target, "diff-tree", "--no-commit-id", "--name-only", "-r", report.commit_sha or ""
    ).splitlines() == [b"src/t001.py"]


def test_repair_policy_accepts_only_zero_one_or_two(tmp_path) -> None:
    target = _repair_target(tmp_path)
    with pytest.raises(RepairPolicyError):
        _runner(
            target,
            tmp_path / "runtime",
            RepairAgent(),
            SequentialChangeProvider(),
            max_repair_cycles=3,
        )


def test_recorded_repair_result_is_reapplied_without_provider_reinvocation(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RepairAgent()
    changes = SequentialChangeProvider()
    first = _runner(
        target,
        runtime,
        agent,
        changes,
        max_repair_cycles=1,
        fault=StageFault("after_node_result_recorded", 11),
    )
    with pytest.raises(RuntimeError, match="M009 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    assert len(changes.requests) == 2

    restarted = _runner(target, runtime, agent, changes, max_repair_cycles=1)
    report = restarted.resume("run_m009_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes[0] == "VALIDATE"
    assert len(changes.requests) == 2
    assert agent.calls.count("classify_failure") == 1


def test_clean_restart_after_repair_resumes_at_validate(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RepairAgent()
    changes = SequentialChangeProvider()
    first = _runner(
        target,
        runtime,
        agent,
        changes,
        max_repair_cycles=1,
        fault=StageFault("after_transition_committed", 11),
    )
    with pytest.raises(RuntimeError, match="M009 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))

    restarted = _runner(target, runtime, agent, changes, max_repair_cycles=1)
    report = restarted.resume("run_m009_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes[0] == "VALIDATE"
    assert len(changes.requests) == 2
    assert agent.calls.count("classify_failure") == 1


def test_target_drift_before_repair_prevents_provider_invocation(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RepairAgent()
    changes = SequentialChangeProvider()
    first = _runner(
        target,
        runtime,
        agent,
        changes,
        max_repair_cycles=1,
        fault=StageFault("after_transition_committed", 10),
    )
    with pytest.raises(RuntimeError, match="M009 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    assert len(changes.requests) == 1
    (target / "tracked.txt").write_text("target drift\n", encoding="utf-8")
    git(target, "add", "tracked.txt")
    git(target, "commit", "--quiet", "-m", "external target drift")

    report = _runner(target, runtime, agent, changes, max_repair_cycles=1).resume(
        "run_m009_fixture"
    )

    assert report.outcome in {WriteSliceOutcome.BLOCKED, WriteSliceOutcome.FAILED}
    assert len(changes.requests) == 1
    assert report.graph_state is not None
    assert report.graph_state.graph.previous_node in {"DEBUGGER", "FINALIZE"}
    assert not (
        Path(report.runtime_path or "") / "operations" / "repairs" / "001" / "proposal.json"
    ).exists()


def test_interrupted_repair_node_is_blocked_and_never_reinvoked(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RepairAgent()
    changes = SequentialChangeProvider()
    first = _runner(
        target,
        runtime,
        agent,
        changes,
        max_repair_cycles=1,
        fault=StageFault("after_node_started", 11),
    )
    with pytest.raises(RuntimeError, match="M009 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    assert len(changes.requests) == 1

    assessment = first.assess_recovery("run_m009_fixture")
    assert assessment.action is RecoveryAction.BLOCKED
    assert assessment.reason_code == "unreconciled_side_effect_capability"
    report = _runner(target, runtime, agent, changes, max_repair_cycles=1).resume(
        "run_m009_fixture"
    )

    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.issues[0].code == "unreconciled_side_effect_capability"
    assert len(changes.requests) == 1


def test_interrupted_classifier_reruns_with_distinct_attempt_evidence(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RepairAgent(("debugger", "debugger"))
    changes = SequentialChangeProvider()
    first = _runner(
        target,
        runtime,
        agent,
        changes,
        max_repair_cycles=1,
        fault=StageFault("after_node_invocation", 10),
    )
    with pytest.raises(RuntimeError, match="M009 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))

    restarted = _runner(target, runtime, agent, changes, max_repair_cycles=1)
    report = restarted.resume("run_m009_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes[0] == "CLASSIFY_FAILURE"
    assert agent.calls.count("classify_failure") == 2
    attempts = (
        Path(report.runtime_path or "") / "provider" / "m009-fake" / "agents" / "CLASSIFY_FAILURE"
    )
    assert len(tuple(attempts.iterdir())) == 2


def test_repair_manifest_tamper_requires_recovery_without_reinvocation(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RepairAgent()
    changes = SequentialChangeProvider()
    first = _runner(
        target,
        runtime,
        agent,
        changes,
        max_repair_cycles=1,
        fault=StageFault("after_transition_committed", 11),
    )
    with pytest.raises(RuntimeError, match="M009 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    manifest_path = (
        first.paths.run("prj_m009_fixture", "run_m009_fixture")
        / "operations"
        / "repairs"
        / "001"
        / "workspace-manifest.json"
    )
    manifest_path.write_bytes(manifest_path.read_bytes().replace(b'"cycle":1', b'"cycle":2', 1))

    report = _runner(target, runtime, agent, changes, max_repair_cycles=1).resume(
        "run_m009_fixture"
    )

    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.issues[0].code == "repair_lineage_mismatch"
    assert len(changes.requests) == 2


def test_repair_context_tamper_stops_before_change_provider(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    agent = RepairAgent()
    changes = SequentialChangeProvider()
    first = _runner(
        target,
        runtime,
        agent,
        changes,
        max_repair_cycles=1,
        fault=StageFault("after_node_result_recorded", 10),
    )
    with pytest.raises(RuntimeError, match="M009 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    context_path = (
        first.paths.run("prj_m009_fixture", "run_m009_fixture")
        / "operations"
        / "repairs"
        / "001"
        / "context.json"
    )
    context_path.write_bytes(context_path.read_bytes().replace(b'"cycle":1', b'"cycle":2', 1))

    report = _runner(target, runtime, agent, changes, max_repair_cycles=1).resume(
        "run_m009_fixture"
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "repair_failure_context_mismatch"
    assert len(changes.requests) == 1
