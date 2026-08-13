from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.agents import AgentResponse
from agentgraph.core import FailureCategory, RepairClassification, RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.runtime.codec import canonical_json_bytes, parse_json_bytes, sha256_digest
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


class ReviewBoundaryCrash(BaseException):
    pass


class CrashAfterSemanticEvidence:
    def __init__(self):
        self.triggered = False

    def __call__(self, stage):
        if stage == "after_semantic_review_evidence" and not self.triggered:
            self.triggered = True
            raise ReviewBoundaryCrash("crash before combined review evidence")


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


def rewrite_evidence(path: Path, mutate) -> None:
    document = parse_json_bytes(path.read_bytes())
    mutate(document)
    body = {key: value for key, value in document.items() if key != "content_digest"}
    document["content_digest"] = "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    atomic_write_bytes(path, canonical_json_bytes(document))


def crash_after_completed_review(tmp_path, reviewer, *, changes=None, repairs=0, invocation=10):
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    selected_changes = changes or SequentialChangeProvider((2,))
    first = runner(
        target,
        runtime,
        RepairAgent(("debugger", "programmer")),
        selected_changes,
        reviewer,
        repairs=repairs,
        fault=NodeInvocationFault(invocation),
    )
    with pytest.raises(RuntimeError, match="M010 interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    return target, runtime, first


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


def test_finalized_semantic_review_rehydrates_without_reviewer_reinvoke(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = SemanticReviewer()
    changes = SequentialChangeProvider((2,))
    first = runner(target, runtime, RepairAgent(), changes, reviewer)
    initial = first.run(WriteSliceRequest(scope_id="E001"))
    assert initial.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED

    resumed = runner(target, runtime, RepairAgent(), changes, reviewer).resume("run_m010_fixture")

    assert resumed.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert len(reviewer.calls) == 1


def test_completed_fail_is_reused_then_routes_to_classifier_without_same_cycle_reinvoke(
    tmp_path,
) -> None:
    reviewer = SemanticReviewer(("fail", "pass"))
    changes = SequentialChangeProvider((2, 2))
    target, runtime, first = crash_after_completed_review(
        tmp_path, reviewer, changes=changes, repairs=1
    )
    first_digest = reviewer.calls[0].input_digest

    report = runner(
        target,
        runtime,
        RepairAgent(("debugger",)),
        first.change_provider,
        reviewer,
        repairs=1,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes[:2] == ("REVIEW", "CLASSIFY_FAILURE")
    assert len(reviewer.calls) == 2
    assert sum(call.input_digest == first_digest for call in reviewer.calls) == 1


def test_completed_blocked_review_is_reused_without_reviewer_reinvoke(tmp_path) -> None:
    reviewer = SemanticReviewer(blocked=True)
    target, runtime, first = crash_after_completed_review(tmp_path, reviewer)

    report = runner(
        target,
        runtime,
        RepairAgent(),
        first.change_provider,
        reviewer,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "review_evidence_unavailable"
    assert len(reviewer.calls) == 1


def tamper_semantic_host(runtime: Path) -> None:
    path = next(runtime.rglob("provider/m010-reviewer/agents/REVIEW/*/analysis.json"))
    rewrite_evidence(
        path,
        lambda document: document["payload"]["response"].__setitem__("summary", "tampered summary"),
    )


def tamper_review_context(runtime: Path) -> None:
    path = next(runtime.rglob("operations/review-context.json"))
    rewrite_evidence(
        path,
        lambda document: document["payload"]["context"].__setitem__("goal", "tampered goal"),
    )


def tamper_final_review(runtime: Path) -> None:
    path = next(runtime.rglob("operations/review.json"))
    rewrite_evidence(
        path,
        lambda document: document["payload"].__setitem__("passed", False),
    )


def substitute_semantic_reference(runtime: Path) -> None:
    path = next(runtime.rglob("operations/review.json"))
    rewrite_evidence(
        path,
        lambda document: document["payload"]["semantic"].__setitem__(
            "evidence_reference", "provider/m010-reviewer/agents/REVIEW/missing/analysis.json"
        ),
    )


def substitute_semantic_input_digest(runtime: Path) -> None:
    path = next(runtime.rglob("operations/review.json"))
    rewrite_evidence(
        path,
        lambda document: document["payload"]["semantic"].__setitem__(
            "input_digest", "sha256:" + "0" * 64
        ),
    )


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    [
        (tamper_semantic_host, "semantic_review_evidence_mismatch"),
        (tamper_review_context, "semantic_review_context_mismatch"),
        (tamper_final_review, "semantic_review_evidence_mismatch"),
        (substitute_semantic_reference, "semantic_review_evidence_mismatch"),
        (substitute_semantic_input_digest, "semantic_review_evidence_mismatch"),
    ],
)
def test_completed_review_tamper_is_recovery_corruption_without_reviewer_call(
    tmp_path, tamper, expected_code
) -> None:
    reviewer = SemanticReviewer()
    target, runtime, first = crash_after_completed_review(tmp_path, reviewer)
    tamper(runtime)

    report = runner(
        target,
        runtime,
        RepairAgent(),
        first.change_provider,
        reviewer,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.issues[0].code == expected_code
    assert len(reviewer.calls) == 1


def test_previous_cycle_semantic_evidence_cannot_authorize_current_cycle(tmp_path) -> None:
    reviewer = SemanticReviewer(("fail", "pass"))
    changes = SequentialChangeProvider((2, 2))
    target, runtime, first = crash_after_completed_review(
        tmp_path,
        reviewer,
        changes=changes,
        repairs=1,
        invocation=14,
    )
    cycle_zero = read_evidence(next(runtime.rglob("operations/review.json")))["payload"]
    cycle_one_path = next(runtime.rglob("operations/repairs/001/review.json"))

    def substitute(document):
        current = document["payload"]["semantic"]
        old = cycle_zero["semantic"]
        current["evidence_reference"] = old["evidence_reference"]
        current["input_digest"] = old["input_digest"]

    rewrite_evidence(cycle_one_path, substitute)

    report = runner(
        target,
        runtime,
        RepairAgent(),
        first.change_provider,
        reviewer,
        repairs=1,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.issues[0].code == "semantic_review_evidence_mismatch"
    assert len(reviewer.calls) == 2


def test_partial_semantic_evidence_is_not_completion_authority(tmp_path) -> None:
    target = _repair_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = SemanticReviewer(("pass", "pass"))
    crash = CrashAfterSemanticEvidence()
    first = runner(
        target,
        runtime,
        RepairAgent(),
        SequentialChangeProvider((2,)),
        reviewer,
        fault=crash,
    )

    with pytest.raises(ReviewBoundaryCrash):
        first.run(WriteSliceRequest(scope_id="E001"))
    assert not tuple(runtime.rglob("operations/review.json"))
    assert len(tuple(runtime.rglob("provider/m010-reviewer/agents/REVIEW/*/analysis.json"))) == 1

    report = runner(
        target,
        runtime,
        RepairAgent(),
        first.change_provider,
        reviewer,
    ).resume("run_m010_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert len(reviewer.calls) == 2
    assert reviewer.contexts[0].node_attempt_id == reviewer.contexts[1].node_attempt_id
    assert (
        reviewer.contexts[0].provider_invocation_id != reviewer.contexts[1].provider_invocation_id
    )


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
