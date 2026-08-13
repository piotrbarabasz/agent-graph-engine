from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.agents import AgentResponse
from agentgraph.core import FailureCategory, RepairClassification, RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.runtime.codec import sha256_digest
from agentgraph.write import WriteSliceOutcome, WriteSliceRequest, WriteSliceRunner
from agentgraph.write.evidence import read_evidence
from tests.integration.test_m009_repair_loop import (
    RepairAgent,
    SequentialChangeProvider,
    _repair_target,
)


class SemanticReviewer:
    evidence_namespace = "m010-reviewer"

    def __init__(self, verdicts=("pass",), *, mutate=None, malformed=False, blocked=False):
        self.verdicts = iter(verdicts)
        self.mutate = mutate
        self.malformed = malformed
        self.blocked = blocked
        self.calls = []
        self.contexts = []

    def invoke(self, request, context):
        assert request.operation_id == "semantic_review"
        self.calls.append(request)
        self.contexts.append(context)
        if self.mutate is not None:
            self.mutate(context)
        if self.blocked:
            value = {
                "schema_version": 1,
                "status": "blocked",
                "verdict": None,
                "summary": None,
                "findings": [],
                "reason_code": "review_evidence_unavailable",
                "message": "The workspace cannot be assessed safely.",
            }
        else:
            verdict = next(self.verdicts)
            value = {
                "schema_version": 1,
                "status": "success",
                "verdict": verdict,
                "summary": "Material semantic assessment.",
                "findings": []
                if verdict == "pass"
                else [
                    {
                        "kind": "acceptance_criterion_failure",
                        "path": "README.md",
                        "message": "The implementation does not satisfy AC-M010.",
                        "requirement_refs": ["AC-M010"],
                    }
                ],
                "reason_code": None,
                "message": None,
            }
        if self.malformed:
            value["safe_to_close"] = True
        return AgentResponse(
            value,
            "m010-fake-reviewer",
            "1",
            None,
            request.input_digest,
            sha256_digest(value),
            "response.json",
        )


class NodeInvocationFault:
    def __init__(self, target):
        self.target = target
        self.count = 0

    def __call__(self, stage):
        if stage == "after_node_invocation":
            self.count += 1
            if self.count == self.target:
                raise RuntimeError("injected M010 interruption")


class MutateAfterValidation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.count = 0

    def __call__(self, stage):
        if stage != "after_node_invocation":
            return
        self.count += 1
        if self.count == 9:
            workspace_file = next(self.runtime.rglob("workspace/src/t001.py"))
            workspace_file.write_text("value = 777\n", encoding="utf-8")


def runner(target, runtime, analysis, changes, review=None, *, repairs=0, fault=None):
    paths = RuntimePaths.resolve(runtime)
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        changes,
        agent_provider=analysis,
        review_agent_provider=review,
        git_adapter=adapter,
        project_registry=ProjectRegistry(paths, project_id_factory=lambda: "prj_m010_fixture"),
        commit_identity=GitCommitIdentity("M010 Test", "m010@example.test"),
        run_id_factory=lambda: "run_m010_fixture",
        max_repair_cycles=repairs,
        fault=fault,
    )


def test_semantic_pass_runs_inside_canonical_review_and_commits_once(tmp_path) -> None:
    target = _repair_target(tmp_path)
    reviewer = SemanticReviewer()
    report = runner(
        target,
        tmp_path / "runtime",
        RepairAgent(),
        SequentialChangeProvider((2,)),
        reviewer,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes.count("REVIEW") == 1
    assert "SEMANTIC_REVIEW" not in report.executed_nodes
    assert len(reviewer.calls) == 1
    assert reviewer.contexts[0].repository_root.name == "workspace"
    assert report.graph_state is not None
    assert report.graph_state.review.safe_to_close is True
    assert report.graph_state.repair.count == 0
    operation = read_evidence(next((tmp_path / "runtime").rglob("operations/review.json")))[
        "payload"
    ]
    assert operation["mechanical"]["passed"] is True
    assert operation["semantic_review_enabled"] is True
    assert operation["semantic"]["verdict"] == "pass"


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        ("debugger", RepairClassification.DEBUGGER),
        ("programmer", RepairClassification.PROGRAMMER),
    ],
)
def test_semantic_fail_uses_existing_classifier_and_repair_then_fresh_pass(
    tmp_path, route, expected
) -> None:
    target = _repair_target(tmp_path)
    analysis = RepairAgent((route,))
    reviewer = SemanticReviewer(("fail", "pass"))
    changes = SequentialChangeProvider((2, 2))

    report = runner(target, tmp_path / "runtime", analysis, changes, reviewer, repairs=1).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state is not None
    assert report.graph_state.repair.count == 1
    assert report.graph_state.repair.history[0].classification is expected
    assert report.executed_nodes.count("REVIEW") == 2
    assert len(reviewer.calls) == 2
    assert "The implementation does not satisfy AC-M010." not in reviewer.calls[1].prompt
    repair_request = changes.requests[1]
    assert repair_request.review_findings == (
        "semantic:acceptance_criterion_failure:README.md:"
        "The implementation does not satisfy AC-M010. [requirements: AC-M010]",
    )
    assert "README.md" not in tuple(path.path for path in repair_request.allowed_paths)


def test_two_semantic_failures_use_two_repairs_then_one_final_commit(tmp_path) -> None:
    target = _repair_target(tmp_path)
    analysis = RepairAgent(("debugger", "programmer"))
    reviewer = SemanticReviewer(("fail", "fail", "pass"))
    changes = SequentialChangeProvider((2, 2, 2))

    report = runner(target, tmp_path / "runtime", analysis, changes, reviewer, repairs=2).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state is not None
    assert report.graph_state.repair.count == 2
    assert tuple(record.classification for record in report.graph_state.repair.history) == (
        RepairClassification.DEBUGGER,
        RepairClassification.PROGRAMMER,
    )
    assert len(reviewer.calls) == 3
    assert report.executed_nodes.count("REVIEW") == 3


def test_semantic_fail_without_capacity_does_not_commit(tmp_path) -> None:
    target = _repair_target(tmp_path)
    reviewer = SemanticReviewer(("fail",))
    report = runner(
        target,
        tmp_path / "runtime",
        RepairAgent(),
        SequentialChangeProvider((2,)),
        reviewer,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.commit_sha is None
    assert report.graph_state is not None
    assert report.graph_state.failure.code == "semantic_review_failed"
    assert report.graph_state.failure.category is FailureCategory.VALIDATION


def test_mechanical_failure_skips_semantic_reviewer(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = SemanticReviewer()
    report = runner(
        target,
        runtime,
        RepairAgent(),
        SequentialChangeProvider((2,)),
        reviewer,
        fault=MutateAfterValidation(runtime),
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.FAILED
    assert reviewer.calls == []
    assert report.graph_state is not None
    assert report.graph_state.failure.code == "deterministic_review_failed"
    review = read_evidence(next(runtime.rglob("operations/review.json")))["payload"]
    assert review["mechanical"]["passed"] is False
    assert review["semantic"] is None


def test_blocked_semantic_reviewer_finalizes_blocked(tmp_path) -> None:
    target = _repair_target(tmp_path)
    report = runner(
        target,
        tmp_path / "runtime",
        RepairAgent(),
        SequentialChangeProvider((2,)),
        SemanticReviewer(blocked=True),
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.BLOCKED
    assert report.issues[0].code == "review_evidence_unavailable"


def test_malformed_semantic_response_is_fatal_contract_failure(tmp_path) -> None:
    target = _repair_target(tmp_path)
    report = runner(
        target,
        tmp_path / "runtime",
        RepairAgent(),
        SequentialChangeProvider((2,)),
        SemanticReviewer(malformed=True),
        repairs=2,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.graph_state is not None
    assert report.issues[0].code == "agent_response_contract_invalid"
    assert "CLASSIFY_FAILURE" not in report.executed_nodes


def test_same_dirty_file_reviewer_mutation_is_detected_without_repair(tmp_path) -> None:
    target = _repair_target(tmp_path)

    def mutate(context):
        (context.repository_root / "src" / "t001.py").write_text("value = 999\n", encoding="utf-8")

    report = runner(
        target,
        tmp_path / "runtime",
        RepairAgent(),
        SequentialChangeProvider((2,)),
        SemanticReviewer(mutate=mutate),
        repairs=2,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "agent_provider_mutated_repository"
    assert "CLASSIFY_FAILURE" not in report.executed_nodes
    assert report.workspace_path is not None and Path(report.workspace_path).exists()


def test_completed_review_evidence_is_reused_after_interruption(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = SemanticReviewer()
    fault = NodeInvocationFault(10)
    first = runner(
        target,
        runtime,
        RepairAgent(),
        SequentialChangeProvider((2,)),
        reviewer,
        fault=fault,
    )
    with pytest.raises(RuntimeError, match="M010 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    assert len(reviewer.calls) == 1

    report = runner(
        target,
        runtime,
        RepairAgent(),
        first.change_provider,
        reviewer,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert len(reviewer.calls) == 1


def test_enabled_resume_requires_review_provider(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = SemanticReviewer()
    first = runner(
        target,
        runtime,
        RepairAgent(),
        SequentialChangeProvider((2,)),
        reviewer,
        fault=NodeInvocationFault(10),
    )
    with pytest.raises(RuntimeError):
        first.run(WriteSliceRequest(scope_id="E001"))

    report = runner(
        target,
        runtime,
        RepairAgent(),
        first.change_provider,
        None,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.issues[0].code == "review_provider_required"


def test_provider_added_on_legacy_resume_does_not_enable_semantic_review(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    fault = NodeInvocationFault(10)
    first = runner(
        target,
        runtime,
        RepairAgent(),
        SequentialChangeProvider((2,)),
        None,
        fault=fault,
    )
    with pytest.raises(RuntimeError):
        first.run(WriteSliceRequest(scope_id="E001"))
    reviewer = SemanticReviewer()

    report = runner(
        target,
        runtime,
        RepairAgent(),
        first.change_provider,
        reviewer,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert reviewer.calls == []
