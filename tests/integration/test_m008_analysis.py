from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.agents import AgentResponse
from agentgraph.core import RiskLevel
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.runtime.codec import sha256_digest
from agentgraph.write import (
    ChangeSet,
    FileChange,
    WriteSliceOutcome,
    WriteSliceRequest,
    WriteSliceRunner,
)
from tests.integration.conftest import git, semantic_git_state, working_tree_bytes
from tests.integration.test_m006_vertical_slice import _target


class RecordingAgentProvider:
    evidence_namespace = "fake"

    def __init__(
        self,
        *,
        recommended=("src/t001.py",),
        risk="medium",
        checkpoint=False,
        transform=None,
        mutate=None,
    ):
        self.requests = []
        self.recommended = recommended
        self.risk = risk
        self.checkpoint = checkpoint
        self.transform = transform
        self.mutate = mutate

    def invoke(self, request, context):
        self.requests.append((request, context))
        if request.operation_id == "explore":
            payload = {
                "schema_version": 1,
                "status": "success",
                "relevant_files": ["tracked.txt", "specs/one/tasks.md"],
                "architecture_observations": ["The target keeps declared work under specs."],
                "derived_requirements": ["Preserve the tracked baseline."],
                "derived_acceptance_criteria": ["The new module remains deterministic."],
                "derived_constraints": ["Do not modify source declarations."],
                "architecture_invariants": ["Target repository remains read-only."],
                "uncertainties": [],
                "reason_code": None,
                "message": None,
            }
        elif request.operation_id == "build_task_package":
            payload = {
                "schema_version": 1,
                "status": "success",
                "objective": "Implement T001 in its declared file.",
                "implementation_steps": ["Create src/t001.py with deterministic content."],
                "recommended_change_paths": list(self.recommended),
                "supporting_read_paths": ["tracked.txt"],
                "validation_focus": ["Run the declared checks only in VALIDATE."],
                "assumptions": [],
                "unresolved_questions": [],
                "reason_code": None,
                "message": None,
            }
        else:
            payload = {
                "schema_version": 1,
                "status": "success",
                "risk_level": self.risk,
                "reasons": ["Bounded single-file change."],
                "sensitive_areas": [],
                "destructive_change_concerns": [],
                "requests_human_checkpoint": self.checkpoint,
                "reason_code": None,
                "message": None,
            }
        if self.transform is not None:
            payload = self.transform(request.operation_id, payload)
        if self.mutate is not None:
            self.mutate(request.operation_id, context)
        return AgentResponse(
            payload,
            "fake-agent",
            "1",
            None,
            request.input_digest,
            sha256_digest(payload),
            "fake-response.json",
        )


class CapturingChangeProvider:
    def __init__(self):
        self.requests = []

    def propose(self, request, context):
        self.requests.append(request)
        return ChangeSet.create((FileChange("src/t001.py", None, "value = 8\n"),))


def runner(target: Path, runtime: Path, agent, change=None, fault=None):
    paths = RuntimePaths.resolve(runtime)
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        change or CapturingChangeProvider(),
        agent_provider=agent,
        git_adapter=adapter,
        project_registry=ProjectRegistry(paths, project_id_factory=lambda: "prj_m008_fixture"),
        commit_identity=GitCommitIdentity("M008 Test", "m008@example.test"),
        run_id_factory=lambda: "run_m008_fixture",
        fault=fault,
    )


def test_full_agent_path_is_advisory_and_enriches_implement(tmp_path) -> None:
    target = _target(tmp_path)
    before_git = semantic_git_state(target)
    before_bytes = working_tree_bytes(target)
    agent = RecordingAgentProvider()
    change = CapturingChangeProvider()

    report = runner(target, tmp_path / "runtime", agent, change).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert [request.operation_id for request, _ in agent.requests] == [
        "explore",
        "build_task_package",
        "assess_risk",
    ]
    assert len(change.requests) == 1
    request = change.requests[0]
    assert tuple(path.path for path in request.allowed_paths) == (
        "src/t001.py",
        "tests/t001.py",
    )
    assert request.analysis_summary == ("The target keeps declared work under specs.",)
    assert request.implementation_plan == ("Create src/t001.py with deterministic content.",)
    assert request.derived_constraints == ("Do not modify source declarations.",)
    assert request.validation_focus == ("Run the declared checks only in VALIDATE.",)
    assert "Preserve the tracked baseline." in report.graph_state.requirements.items
    assert "The new module remains deterministic." in report.graph_state.acceptance_criteria.items
    assert semantic_git_state(target) == before_git
    assert working_tree_bytes(target) == before_bytes


def test_task_package_cannot_expand_scope_before_implement(tmp_path) -> None:
    target = _target(tmp_path)
    agent = RecordingAgentProvider(recommended=("README.md",))
    change = CapturingChangeProvider()

    report = runner(target, tmp_path / "runtime", agent, change).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "agent_task_package_scope_expansion"
    assert change.requests == []
    assert [request.operation_id for request, _ in agent.requests] == [
        "explore",
        "build_task_package",
    ]


def test_agent_risk_escalates_and_checkpoint_blocks_implement(tmp_path) -> None:
    target = _target(tmp_path)
    agent = RecordingAgentProvider(risk="critical")
    change = CapturingChangeProvider()

    report = runner(target, tmp_path / "runtime", agent, change).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "human_checkpoint_required_not_supported_in_m008"
    assert change.requests == []
    assert not (Path(report.runtime_path or "") / "workspace").exists()


def test_agent_checkpoint_request_blocks_even_below_critical(tmp_path) -> None:
    target = _target(tmp_path)
    change = CapturingChangeProvider()

    report = runner(
        target,
        tmp_path / "runtime",
        RecordingAgentProvider(risk="medium", checkpoint=True),
        change,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "human_checkpoint_required_not_supported_in_m008"
    assert change.requests == []


def test_source_risk_is_a_hard_lower_bound(tmp_path) -> None:
    target = _target(tmp_path)
    tasks = target / "specs" / "one" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace("Risk: medium", "Risk: high"),
        encoding="utf-8",
    )
    git(target, "add", "--", "specs/one/tasks.md")
    git(target, "commit", "--quiet", "-m", "raise source risk")

    report = runner(target, tmp_path / "runtime", RecordingAgentProvider(risk="low")).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state.risk.level is RiskLevel.HIGH


def test_agent_can_escalate_source_risk(tmp_path) -> None:
    target = _target(tmp_path)

    report = runner(target, tmp_path / "runtime", RecordingAgentProvider(risk="high")).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state.risk.level is RiskLevel.HIGH


def test_source_requirements_and_acceptance_cannot_disappear(tmp_path) -> None:
    target = _target(tmp_path)

    report = runner(target, tmp_path / "runtime", RecordingAgentProvider()).run(
        WriteSliceRequest(scope_id="E001")
    )

    requirements = report.graph_state.requirements.items
    acceptance = report.graph_state.acceptance_criteria.items
    assert "Inspect this item without executing it." in requirements
    assert "Assert the read-only boundary." in requirements
    assert "Preserve the tracked baseline." in requirements
    assert "Selection and projection remain deterministic." in acceptance
    assert "The new module remains deterministic." in acceptance


@pytest.mark.parametrize(
    ("role", "expected_calls"),
    (
        ("explore", ("explore",)),
        ("build_task_package", ("explore", "build_task_package")),
        ("assess_risk", ("explore", "build_task_package", "assess_risk")),
    ),
)
def test_malformed_role_response_fails_without_next_agent_or_implement(
    tmp_path, role, expected_calls
) -> None:
    target = _target(tmp_path)

    def malformed(operation, payload):
        if operation == role:
            return {**payload, "next_node": "IMPLEMENT"}
        return payload

    agent = RecordingAgentProvider(transform=malformed)
    change = CapturingChangeProvider()
    report = runner(target, tmp_path / "runtime", agent, change).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "agent_response_contract_invalid"
    assert tuple(request.operation_id for request, _ in agent.requests) == expected_calls
    assert change.requests == []


def test_explicit_blocked_explore_uses_canonical_blocked_path_without_retry(tmp_path) -> None:
    target = _target(tmp_path)

    def blocked(operation, payload):
        if operation == "explore":
            return {
                **payload,
                "status": "blocked",
                "reason_code": "insufficient_repository_context",
                "message": "Required repository context is absent.",
            }
        return payload

    agent = RecordingAgentProvider(transform=blocked)
    change = CapturingChangeProvider()
    report = runner(target, tmp_path / "runtime", agent, change).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "insufficient_repository_context"
    assert len(agent.requests) == 1
    assert change.requests == []


def test_explore_read_paths_do_not_change_write_capability(tmp_path) -> None:
    target = _target(tmp_path)
    agent = RecordingAgentProvider(recommended=())
    change = CapturingChangeProvider()

    report = runner(target, tmp_path / "runtime", agent, change).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert tuple(path.path for path in change.requests[0].allowed_paths) == (
        "src/t001.py",
        "tests/t001.py",
    )
    assert "tracked.txt" in change.requests[0].relevant_files
    assert all(path.path != "tracked.txt" for path in change.requests[0].allowed_paths)
