from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.agents import AgentResponse
from agentgraph.core import CheckpointOutcome, ReviewVerdict, RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths, StateStore
from agentgraph.runtime.codec import decode_value, sha256_digest
from agentgraph.write import (
    CheckpointError,
    WriteSliceOutcome,
    WriteSliceRequest,
    WriteSliceRunner,
)
from agentgraph.write.evidence import read_evidence
from agentgraph.write.models import DeliveryReviewContext
from tests.integration.conftest import git
from tests.integration.test_m010_semantic_review import SemanticReviewer
from tests.integration.test_m012_multi_item import (
    PerItemProvider,
    StageFault,
    _item_roots,
    _multi_target,
    _rewrite_evidence,
    _symlink,
)


class DeliveryReviewer:
    evidence_namespace = "m013-delivery-reviewer"

    def __init__(
        self,
        verdict: str = "pass",
        *,
        blocked: bool = False,
        malformed=False,
        mutate=None,
        unknown_item: bool = False,
        wrong_output_digest: bool = False,
    ):
        self.verdict = verdict
        self.blocked = blocked
        self.malformed = malformed
        self.mutate = mutate
        self.unknown_item = unknown_item
        self.wrong_output_digest = wrong_output_digest
        self.calls = []
        self.contexts = []

    def invoke(self, request, context):
        self.calls.append(request)
        self.contexts.append(context)
        context.runtime_directory.mkdir(parents=True, exist_ok=False)
        if self.malformed:
            payload = {"schema_version": 1, "status": "success"}
        elif self.blocked:
            payload = {
                "schema_version": 1,
                "status": "blocked",
                "verdict": None,
                "summary": None,
                "findings": [],
                "reason_code": "reviewer_unavailable",
                "message": "delivery reviewer unavailable",
            }
        else:
            findings = []
            summary = "No material delivery defect was identified."
            if self.verdict == "fail":
                summary = "The final items do not integrate correctly."
                findings = [
                    {
                        "kind": "cross_item_integration_failure",
                        "message": "The combined behavior violates the scope contract.",
                        "path": "src/t002.py",
                        "item_ids": ["T001", "T002"],
                        "requirement_refs": ["scope integration"],
                    }
                ]
            if self.unknown_item:
                findings = [
                    {
                        "kind": "incomplete_delivery",
                        "message": "Unknown item reference.",
                        "path": None,
                        "item_ids": ["T999"],
                        "requirement_refs": [],
                    }
                ]
                self.verdict = "fail"
                summary = "The response contains an unknown item reference."
            payload = {
                "schema_version": 1,
                "status": "success",
                "verdict": self.verdict,
                "summary": summary,
                "findings": findings,
                "reason_code": None,
                "message": None,
            }
        if self.mutate is not None:
            self.mutate(context)
        return AgentResponse(
            payload,
            "m013-test-reviewer",
            "1",
            None,
            request.input_digest,
            "sha256:" + "0" * 64 if self.wrong_output_digest else sha256_digest(payload),
            "response.json",
        )


class NamedFault:
    def __init__(self, name: str) -> None:
        self.name = name
        self.triggered = False

    def __call__(self, stage: str) -> None:
        if stage == self.name and not self.triggered:
            self.triggered = True
            raise RuntimeError("injected M013 interruption")


def _runner(
    target: Path,
    runtime: Path,
    provider,
    *,
    delivery=None,
    review=None,
    fault=None,
) -> WriteSliceRunner:
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    paths = RuntimePaths.resolve(runtime)
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        provider,
        review_agent_provider=review,
        delivery_review_agent_provider=delivery,
        git_adapter=adapter,
        project_registry=ProjectRegistry(paths, project_id_factory=lambda: "prj_m013"),
        commit_identity=GitCommitIdentity("M013 Test", "m013@example.test"),
        run_id_factory=lambda: "run_m013",
        max_work_items_per_run=3,
        fault=fault,
    )


def test_two_item_delivery_pass_stops_at_unmaterialized_publish_checkpoint(tmp_path) -> None:
    target = _multi_target(tmp_path)
    reviewer = DeliveryReviewer()
    item_reviewer = SemanticReviewer(("pass", "pass"))
    report = _runner(
        target,
        tmp_path / "runtime",
        PerItemProvider(),
        delivery=reviewer,
        review=item_reviewer,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.RUNNING
    assert report.graph_state.graph.current_node == "HUMAN_CHECKPOINT"
    assert report.graph_state.graph.pending_resume_node == "CREATE_PR"
    assert report.graph_state.review.verdict is ReviewVerdict.PASS
    assert report.graph_state.review.safe_to_create_pr is True
    assert report.checkpoint is None
    assert report.delivery_review is not None
    assert report.delivery_review.verdict is ReviewVerdict.PASS
    assert len(item_reviewer.calls) == 2
    assert len(reviewer.calls) == 1
    run = Path(report.runtime_path or "")
    assert not (run / "checkpoints").exists()
    assert not (run / "final.json").exists()
    assert (run / "delivery-review" / "review.json").is_file()
    persisted = read_evidence(run / "run-inputs.json")["payload"]
    assert persisted["delivery_review_enabled"] is True


def test_reverse_dependency_delivery_context_uses_verified_execution_order(tmp_path) -> None:
    target = _multi_target(tmp_path, reverse_dependency=True)
    reviewer = DeliveryReviewer()
    report = _runner(target, tmp_path / "runtime", PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )

    context = read_evidence(Path(report.runtime_path or "") / "delivery-review" / "context.json")[
        "payload"
    ]
    restored = decode_value(context, DeliveryReviewContext)
    assert report.outcome is WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
    assert report.completed_item_ids == ("T002", "T001")
    assert tuple(item.item_id for item in restored.completed_items) == ("T002", "T001")
    assert tuple(item.item_id for item in restored.declared_work) == ("T001", "T002")


def test_delivery_semantic_fail_is_terminal_without_checkpoint_or_repair(tmp_path) -> None:
    target = _multi_target(tmp_path)
    reviewer = DeliveryReviewer("fail")
    report = _runner(target, tmp_path / "runtime", PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.FAILED
    assert report.graph_state.graph.current_node == "END"
    assert report.graph_state.review.verdict is ReviewVerdict.FAIL
    assert report.graph_state.review.safe_to_create_pr is False
    assert "CLASSIFY_FAILURE" not in report.executed_nodes
    assert report.delivery_review is not None
    assert len(report.delivery_review.findings) == 1
    assert len(reviewer.calls) == 1


@pytest.mark.parametrize(
    ("reviewer", "outcome", "code"),
    [
        (DeliveryReviewer(blocked=True), WriteSliceOutcome.BLOCKED, "reviewer_unavailable"),
        (
            DeliveryReviewer(malformed=True),
            WriteSliceOutcome.FAILED,
            "delivery_review_contract_invalid",
        ),
        (
            DeliveryReviewer(wrong_output_digest=True),
            WriteSliceOutcome.FAILED,
            "delivery_review_contract_invalid",
        ),
    ],
)
def test_delivery_provider_blocked_and_malformed_are_typed(
    tmp_path, reviewer, outcome, code
) -> None:
    target = _multi_target(tmp_path)
    report = _runner(target, tmp_path / "runtime", PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )
    assert report.outcome is outcome
    assert report.issues[0].code == code
    assert report.checkpoint is None


def test_delivery_finding_with_unknown_item_is_contract_failure(tmp_path) -> None:
    target = _multi_target(tmp_path)
    reviewer = DeliveryReviewer(unknown_item=True)
    report = _runner(target, tmp_path / "runtime", PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )
    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "delivery_review_contract_invalid"


@pytest.mark.parametrize("mutation", ("tracked", "untracked", "staged", "head", "branch", "mode"))
def test_delivery_reviewer_mutation_never_authorizes_pass(tmp_path, mutation) -> None:
    target = _multi_target(tmp_path)

    def mutate(context):
        path = context.repository_root / "src" / "t001.py"
        if mutation == "tracked":
            path.write_text("mutated = True\n", encoding="utf-8")
        elif mutation == "untracked":
            (context.repository_root / "unexpected.txt").write_text("bad\n", encoding="utf-8")
        elif mutation == "staged":
            path.write_text("mutated = True\n", encoding="utf-8")
            git(context.repository_root, "add", "--", "src/t001.py")
        elif mutation == "head":
            git(context.repository_root, "update-ref", "HEAD", "HEAD^")
        elif mutation == "branch":
            git(context.repository_root, "checkout", "-b", "reviewer-moved")
        else:
            git(context.repository_root, "update-index", "--chmod=+x", "src/t001.py")

    reviewer = DeliveryReviewer(mutate=mutate)
    report = _runner(target, tmp_path / "runtime", PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )
    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "delivery_review_provider_mutated_repository"
    assert report.checkpoint is None


@pytest.mark.parametrize("drift", ("target", "source", "workspace", "scope_ref"))
def test_delivery_mechanical_drift_skips_semantic_provider(tmp_path, drift) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    runner = _runner(
        target,
        runtime,
        PerItemProvider(),
        delivery=reviewer,
        fault=StageFault("after_transition_committed", 21),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    run = RuntimePaths.resolve(runtime).run("prj_m013", "run_m013")
    if drift == "target":
        (target / "tracked.txt").write_text("drift\n", encoding="utf-8")
        git(target, "add", "--", "tracked.txt")
        git(target, "commit", "--quiet", "-m", "target drift")
    elif drift == "source":
        tasks = target / "specs" / "one" / "tasks.md"
        tasks.write_text(tasks.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif drift == "workspace":
        (run / "workspace" / "unexpected.txt").write_text("drift\n", encoding="utf-8")
    else:
        git(target, "update-ref", "refs/heads/work/e001", "HEAD")

    resumed = runner.resume("run_m013")
    expected = (
        WriteSliceOutcome.RECOVERY_REQUIRED if drift == "source" else WriteSliceOutcome.FAILED
    )
    assert resumed.outcome is expected
    assert reviewer.calls == []
    if expected is WriteSliceOutcome.FAILED:
        assert resumed.graph_state is not None
        assert resumed.graph_state.review.verdict is ReviewVerdict.FAIL


def test_mechanical_gate_rejects_cumulative_path_outside_delivery_capability(
    tmp_path, monkeypatch
) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    runner = _runner(
        target,
        runtime,
        PerItemProvider(),
        delivery=reviewer,
        fault=StageFault("after_transition_committed", 21),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    controller, _ = runner._existing_runtime("run_m013")
    state = StateStore(controller.run_path / "state.json").load()
    completed = controller.completed_reports(state)
    workspace = controller.run_path / "workspace"
    (workspace / "outside.py").write_text("outside = True\n", encoding="utf-8")
    git(workspace, "add", "--", "outside.py")
    git(
        workspace,
        "-c",
        "user.name=M013 Test",
        "-c",
        "user.email=m013@example.test",
        "commit",
        "--quiet",
        "-m",
        "forged outside delivery capability",
    )
    forged_head = git(workspace, "rev-parse", "HEAD").strip().decode()
    forged = (*completed[:-1], replace(completed[-1], commit_sha=forged_head))
    monkeypatch.setattr(type(controller), "completed_reports", lambda self, current: forged)
    monkeypatch.setattr(type(controller), "verify_run_boundary", lambda self, current: None)

    _, _, passed, findings = controller.delivery_reviews()._prepare(state)
    assert passed is False
    assert any("delivery_scope_violation" in finding for finding in findings)
    assert reviewer.calls == []


def test_earlier_item_evidence_tamper_prevents_delivery_review(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    runner = _runner(
        target,
        runtime,
        PerItemProvider(),
        delivery=reviewer,
        fault=StageFault("after_transition_committed", 21),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    run = RuntimePaths.resolve(runtime).run("prj_m013", "run_m013")
    witness = _item_roots(run)[0] / "operations" / "commit-witness.json"

    def mutate(document):
        document["scope_id"] = "E999"

    _rewrite_evidence(witness, mutate)
    resumed = runner.resume("run_m013")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert reviewer.calls == []


def test_delivery_checkpoint_resume_is_idempotent_and_submit_is_deferred(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    runner = _runner(target, runtime, PerItemProvider(), delivery=reviewer)
    initial = runner.run(WriteSliceRequest(scope_id="E001"))

    first = runner.resume("run_m013")
    second = runner.resume("run_m013")
    assert first.outcome is second.outcome is WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
    assert len(reviewer.calls) == 1
    assert not (Path(initial.runtime_path or "") / "checkpoints").exists()
    with pytest.raises(CheckpointError) as error:
        runner.submit_checkpoint(
            "run_m013",
            checkpoint_id="not-created",
            nonce="not-created",
            outcome=CheckpointOutcome.APPROVED,
            actor="m013-test",
        )
    assert error.value.code == "delivery_checkpoint_not_implemented_in_m013"
    without_provider = _runner(target, runtime, PerItemProvider())
    with pytest.raises(CheckpointError) as missing_provider_error:
        without_provider.submit_checkpoint(
            "run_m013",
            checkpoint_id="not-created",
            nonce="not-created",
            outcome=CheckpointOutcome.APPROVED,
            actor="m013-test",
        )
    assert missing_provider_error.value.code == "delivery_checkpoint_not_implemented_in_m013"


def test_legacy_run_stays_disabled_even_when_resume_adds_provider(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    initial = _runner(target, runtime, PerItemProvider()).run(WriteSliceRequest(scope_id="E001"))
    reviewer = DeliveryReviewer()
    resumed = _runner(target, runtime, PerItemProvider(), delivery=reviewer).resume("run_m013")

    assert initial.outcome is resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert reviewer.calls == []
    persisted = read_evidence(Path(initial.runtime_path or "") / "run-inputs.json")["payload"]
    assert persisted["delivery_review_enabled"] is False


def test_enabled_resume_without_delivery_provider_requires_recovery(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    _runner(target, runtime, PerItemProvider(), delivery=DeliveryReviewer()).run(
        WriteSliceRequest(scope_id="E001")
    )
    resumed = _runner(target, runtime, PerItemProvider()).resume("run_m013")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "delivery_review_provider_required"


@pytest.mark.parametrize(
    ("stage", "expected_calls"),
    [
        ("after_delivery_review_provider_evidence", 2),
        ("after_delivery_review_evidence", 1),
    ],
)
def test_delivery_review_crash_recovery_respects_final_marker(
    tmp_path, stage, expected_calls
) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    runner = _runner(
        target,
        runtime,
        PerItemProvider(),
        delivery=reviewer,
        fault=NamedFault(stage),
    )
    with pytest.raises(RuntimeError, match="M013 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))

    resumed = runner.resume("run_m013")
    assert resumed.outcome is WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
    assert len(reviewer.calls) == expected_calls


@pytest.mark.parametrize(
    ("stage", "target"),
    (("after_node_result_recorded", 22), ("after_transition_committed", 22)),
)
def test_delivery_review_protocol_crash_never_reinvokes_completed_reviewer(
    tmp_path, stage, target
) -> None:
    target_root = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    runner = _runner(
        target_root,
        runtime,
        PerItemProvider(),
        delivery=reviewer,
        fault=StageFault(stage, target),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))

    resumed = runner.resume("run_m013")
    assert resumed.outcome is WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
    assert len(reviewer.calls) == 1


@pytest.mark.parametrize("name", ("context.json", "manifest.json", "review.json"))
def test_completed_delivery_evidence_tamper_requires_recovery_without_reinvoke(
    tmp_path, name
) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    initial = _runner(target, runtime, PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )
    path = Path(initial.runtime_path or "") / "delivery-review" / name

    def mutate(document):
        document["scope_id"] = "E999"

    _rewrite_evidence(path, mutate)
    resumed = _runner(target, runtime, PerItemProvider(), delivery=reviewer).resume("run_m013")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert len(reviewer.calls) == 1


def test_completed_delivery_host_evidence_tamper_requires_recovery(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    initial = _runner(target, runtime, PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )
    run = Path(initial.runtime_path or "")
    review = read_evidence(run / "delivery-review" / "review.json")["payload"]
    host = run.joinpath(*review["evidence_reference"].split("/"))

    def mutate(document):
        document["final_head"] = initial.baseline_head

    _rewrite_evidence(host, mutate)
    resumed = _runner(target, runtime, PerItemProvider(), delivery=reviewer).resume("run_m013")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert len(reviewer.calls) == 1


@pytest.mark.parametrize(
    ("selected", "directory"),
    (
        ("root", True),
        ("context.json", False),
        ("manifest.json", False),
        ("review.json", False),
        ("provider", True),
    ),
)
def test_delivery_review_storage_links_fail_closed(tmp_path, selected, directory) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    reviewer = DeliveryReviewer()
    initial = _runner(target, runtime, PerItemProvider(), delivery=reviewer).run(
        WriteSliceRequest(scope_id="E001")
    )
    root = Path(initial.runtime_path or "") / "delivery-review"
    selected_path = root if selected == "root" else root / selected
    external = tmp_path / f"external-{selected}"
    if directory:
        external.mkdir()
        sentinel = external / "sentinel.txt"
        shutil.rmtree(selected_path)
    else:
        sentinel = external
        selected_path.unlink()
    sentinel.write_text("unchanged", encoding="utf-8")
    _symlink(selected_path, external, directory=directory)

    resumed = _runner(target, runtime, PerItemProvider(), delivery=reviewer).resume("run_m013")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_delivery_context_binds_original_baseline_final_head_tree_and_cumulative_paths(
    tmp_path,
) -> None:
    target = _multi_target(tmp_path)
    report = _runner(
        target, tmp_path / "runtime", PerItemProvider(), delivery=DeliveryReviewer()
    ).run(WriteSliceRequest(scope_id="E001"))
    run = Path(report.runtime_path or "")
    manifest = read_evidence(run / "delivery-review" / "manifest.json")["payload"]
    assert manifest["target_baseline_head"] == report.baseline_head
    assert manifest["final_head"] == report.commit_shas[-1]
    assert manifest["final_tree_id"] == (
        git(target, "rev-parse", f"{report.commit_shas[-1]}^{{tree}}").strip().decode()
    )
    assert tuple(item["path"] for item in manifest["changed_files"]) == (
        "src/t001.py",
        "src/t002.py",
    )
