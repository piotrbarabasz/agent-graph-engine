from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.agents import DeclaredWorkAgentProvider
from agentgraph.core import CheckpointOutcome, ReviewVerdict, RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths, StateStore
from agentgraph.runtime.codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
    sha256_digest,
)
from agentgraph.write import (
    ChangeIntent,
    ChangeSet,
    FileChange,
    WriteSliceOutcome,
    WriteSliceRequest,
)
from agentgraph.write.evidence import read_evidence
from agentgraph.write.models import WriteInputs, WriteRunInputs
from agentgraph.write.runner import WriteSliceRunner
from tests.integration.test_m010_semantic_review import SemanticReviewer

from .conftest import git, initialize_target, task_block


class PerItemProvider:
    def __init__(self) -> None:
        self.item_ids: list[str] = []
        self.baselines: list[str] = []

    def propose(self, request, context):
        self.item_ids.append(request.item_id)
        self.baselines.append(context.baseline_head)
        path = f"src/{request.item_id.lower()}.py"
        return ChangeSet.create((FileChange(path, None, f'item = "{request.item_id}"\n'),))


class OverlapProvider(PerItemProvider):
    def propose(self, request, context):
        self.item_ids.append(request.item_id)
        self.baselines.append(context.baseline_head)
        path = context.repository_root / "src" / "shared.py"
        before = None if not path.exists() else hashlib.sha256(path.read_bytes()).hexdigest()
        prior = "" if not path.exists() else path.read_text(encoding="utf-8")
        return ChangeSet.create(
            (FileChange("src/shared.py", before, prior + f"{request.item_id} = True\n"),)
        )


def _multi_target(
    tmp_path: Path,
    *,
    dependency: bool = False,
    reverse_dependency: bool = False,
    count: int = 2,
    repair_validation: bool = False,
    critical_item: str | None = None,
) -> Path:
    target = tmp_path / "target"
    initialize_target(target)
    manifest = target / ".specify" / "workstreams" / "E001.yml"
    item_ids = tuple(f"T{index:03d}" for index in range(1, count + 1))
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "tasks:\n  - T001", "tasks:\n" + "\n".join(f"  - {item}" for item in item_ids)
        ),
        encoding="utf-8",
    )
    tasks = target / "specs" / "one" / "tasks.md"
    safe = "python -c \"print('ok')\""
    blocks = []
    for index, item_id in enumerate(item_ids):
        command = safe
        if repair_validation:
            path = f"src/{item_id.lower()}.py"
            command = (
                'python -c "from pathlib import Path; '
                f"raise SystemExit(0 if 'value = 2' in Path('{path}').read_text() else 7)\""
            )
        block = task_block(
            item_id,
            owner="E001",
            dependencies=(
                item_ids[1]
                if reverse_dependency and index == 0
                else item_ids[index - 1]
                if dependency and index
                else "None"
            ),
        ).replace(
            "python -c \"from pathlib import Path; Path('executed.txt').write_text('bad')\"",
            command,
        )
        if item_id == critical_item:
            block = block.replace("Risk: medium", "Risk: critical")
        blocks.append(block)
    tasks.write_text("\n".join(blocks), encoding="utf-8")
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "two item scope")
    return target


def _runner(
    target: Path,
    runtime: Path,
    provider,
    *,
    limit: int = 3,
    repairs: int = 0,
    agent=None,
    reviewer=None,
    fault=None,
) -> WriteSliceRunner:
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    paths = RuntimePaths.resolve(runtime)
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        provider,
        agent_provider=agent,
        review_agent_provider=reviewer,
        git_adapter=adapter,
        project_registry=ProjectRegistry(paths, project_id_factory=lambda: "prj_m012"),
        commit_identity=GitCommitIdentity("M012 Test", "m012@example.test"),
        run_id_factory=lambda: "run_m012",
        max_work_items_per_run=limit,
        max_repair_cycles=repairs,
        fault=fault,
    )


class RepairingPerItemProvider:
    def __init__(
        self, *, clean_initial: bool = False, fail_item: str | None = None, observer=None
    ) -> None:
        self.clean_initial = clean_initial
        self.fail_item = fail_item
        self.requests = []
        self.contexts = []
        self.observer = observer

    def propose(self, request, context):
        self.requests.append(request)
        self.contexts.append(context)
        if self.observer is not None:
            self.observer(request, context)
        if request.item_id == self.fail_item:
            raise RuntimeError("terminal provider failure")
        path = f"src/{request.item_id.lower()}.py"
        candidate = context.repository_root.joinpath(*path.split("/"))
        before = hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None
        value = 2 if self.clean_initial or request.intent is not ChangeIntent.IMPLEMENT else 0
        return ChangeSet.create((FileChange(path, before, f"value = {value}\n"),))


class CountingAgent:
    evidence_namespace = "m012-counting-agent"

    def __init__(self) -> None:
        self.delegate = DeclaredWorkAgentProvider()
        self.calls = []
        self.contexts = []

    def invoke(self, request, context):
        self.calls.append(request.operation_id)
        self.contexts.append(context)
        return self.delegate.invoke(request, context)


class StageFault:
    def __init__(self, stage: str, target: int) -> None:
        self.stage = stage
        self.target = target
        self.count = 0

    def __call__(self, stage: str) -> None:
        if stage == self.stage:
            self.count += 1
            if self.count == self.target:
                raise RuntimeError("injected M012 interruption")


def _rewrite_evidence(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    body = {key: value for key, value in document.items() if key != "content_digest"}
    document["content_digest"] = "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    path.write_bytes(canonical_json_bytes(document))


def _rewrite_durable_state(run: Path, mutate) -> None:
    state_path = run / "state.json"
    state = StateStore(state_path).load()
    forged = mutate(state)
    encoded = encode_value(forged)
    digest = sha256_digest(encoded)
    state_path.write_bytes(
        canonical_json_bytes({"store_schema_version": 1, "state_digest": digest, "state": encoded})
    )

    journal_path = run / "journal.jsonl"
    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        if record["record_type"] == "NODE_RESULT_RECORDED" and (
            record["payload"].get("expected_next_state_version") == forged.state_version
        ):
            record["payload"]["expected_next_state_digest"] = digest
        if record["record_type"] == "TRANSITION_COMMITTED" and (
            record["payload"].get("committed_state_version") == forged.state_version
        ):
            record["payload"]["committed_state_digest"] = digest
    previous = None
    for record in records:
        record["previous_checksum"] = previous
        unsigned = {key: value for key, value in record.items() if key != "checksum"}
        record["checksum"] = sha256_digest(unsigned)
        previous = record["checksum"]
    journal_path.write_bytes(b"".join(canonical_json_bytes(record) + b"\n" for record in records))


def _item_roots(run: Path) -> tuple[Path, ...]:
    return tuple(sorted(path.parent for path in (run / "items").glob("*/write-inputs.json")))


def _symlink(path: Path, target: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, path, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable to current user: {exc}")


def test_two_items_form_linear_commits_and_stop_at_delivery_review(tmp_path) -> None:
    target = _multi_target(tmp_path)
    baseline = git(target, "rev-parse", "HEAD").strip().decode()
    provider = PerItemProvider()

    report = _runner(target, tmp_path / "runtime", provider).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.RUNNING
    assert report.graph_state.graph.current_node == "DELIVERY_REVIEW"
    assert report.completed_item_ids == ("T001", "T002")
    assert provider.item_ids == ["T001", "T002"]
    assert len(report.commit_shas) == 2
    assert provider.baselines == [baseline, report.commit_shas[0]]
    assert git(target, "rev-parse", f"{report.commit_shas[0]}^").strip().decode() == baseline
    assert (
        git(target, "rev-parse", f"{report.commit_shas[1]}^").strip().decode()
        == report.commit_shas[0]
    )
    assert git(target, "rev-parse", "main").strip().decode() == baseline
    run = Path(report.runtime_path or "")
    assert len(tuple((run / "items").glob("*/write-inputs.json"))) == 2
    assert len(tuple((run / "items").glob("*/operations/commit.json"))) == 2
    assert not (run / "final.json").exists()
    item_inputs = tuple(
        decode_value(read_evidence(path)["payload"], WriteInputs)
        for path in sorted((run / "items").glob("*/write-inputs.json"))
    )
    assert tuple(value.baseline_head for value in item_inputs) == (
        baseline,
        report.commit_shas[0],
    )
    assert tuple(
        tuple(path.path for path in value.expected_allowed_paths) for value in item_inputs
    ) == (("src/t001.py", "tests/t001.py"), ("src/t002.py", "tests/t002.py"))
    persisted = decode_value(read_evidence(run / "run-inputs.json")["payload"], WriteRunInputs)
    assert persisted.max_work_items_per_run == 3
    assert persisted.target_baseline_head == baseline
    assert persisted.work_plan_digest == item_inputs[0].work_plan_digest


def test_dependency_uses_projected_completion_and_limit_pauses(tmp_path) -> None:
    target = _multi_target(tmp_path, dependency=True)
    provider = PerItemProvider()

    report = _runner(target, tmp_path / "runtime", provider, limit=2).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.completed_item_ids == ("T001", "T002")
    assert provider.item_ids == ["T001", "T002"]
    assert len(report.commit_shas) == 2


def test_reverse_dependency_order_follows_source_selection_and_forms_linear_commits(
    tmp_path,
) -> None:
    target = _multi_target(tmp_path, reverse_dependency=True)
    baseline = git(target, "rev-parse", "HEAD").strip().decode()
    provider = PerItemProvider()

    report = _runner(target, tmp_path / "runtime", provider).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert provider.item_ids == ["T002", "T001"]
    assert report.completed_item_ids == ("T002", "T001")
    assert git(target, "rev-parse", f"{report.commit_shas[0]}^").strip().decode() == baseline
    assert (
        git(target, "rev-parse", f"{report.commit_shas[1]}^").strip().decode()
        == report.commit_shas[0]
    )


def test_delivery_boundary_resume_is_idempotent_and_policy_is_immutable(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    runner = _runner(target, runtime, provider)
    initial = runner.run(WriteSliceRequest(scope_id="E001"))

    resumed = runner.resume("run_m012")
    mismatch = _runner(target, runtime, PerItemProvider(), limit=2).resume("run_m012")

    assert resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert resumed.commit_shas == initial.commit_shas
    assert provider.item_ids == ["T001", "T002"]
    assert mismatch.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert mismatch.issues[0].code == "work_item_policy_mismatch"

    plan_path = Path(initial.runtime_path or "") / "work-plan.json"
    plan_path.write_bytes(
        plan_path.read_bytes().replace(b'"scope_id":"E001"', b'"scope_id":"X001"')
    )
    tampered = runner.resume("run_m012")
    assert tampered.outcome is WriteSliceOutcome.RECOVERY_REQUIRED


def test_later_item_uses_previous_commit_content_as_its_baseline(tmp_path) -> None:
    target = _multi_target(tmp_path)
    tasks = target / "specs" / "one" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8")
        .replace("Implementation files: `src/t001.py`", "Implementation files: `src/shared.py`")
        .replace("Implementation files: `src/t002.py`", "Implementation files: `src/shared.py`"),
        encoding="utf-8",
    )
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "overlapping capabilities")
    provider = OverlapProvider()

    report = _runner(target, tmp_path / "runtime", provider).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    workspace = Path(report.workspace_path or "")
    assert (workspace / "src" / "shared.py").read_text(encoding="utf-8") == (
        "T001 = True\nT002 = True\n"
    )
    assert provider.baselines[1] == report.commit_shas[0]


def test_three_item_limit_invokes_only_first_two_and_pauses(tmp_path) -> None:
    target = _multi_target(tmp_path, count=3)
    provider = PerItemProvider()

    report = _runner(target, tmp_path / "runtime", provider, limit=2).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.PAUSED
    assert provider.item_ids == ["T001", "T002"]
    assert report.completed_item_ids == ("T001", "T002")
    assert len(report.commit_shas) == 2


def test_two_items_each_get_an_isolated_first_repair_cycle(tmp_path) -> None:
    target = _multi_target(tmp_path, repair_validation=True)
    baseline = git(target, "rev-parse", "HEAD").strip().decode()
    provider = RepairingPerItemProvider()

    report = _runner(
        target,
        tmp_path / "runtime",
        provider,
        repairs=1,
        agent=DeclaredWorkAgentProvider(),
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert [item.repair_count for item in report.completed_items] == [1, 1]
    assert len(report.commit_shas) == 2
    assert git(target, "rev-parse", f"{report.commit_shas[0]}^").strip().decode() == baseline
    assert (
        git(target, "rev-parse", f"{report.commit_shas[1]}^").strip().decode()
        == report.commit_shas[0]
    )
    run = Path(report.runtime_path or "")
    first, second = _item_roots(run)
    assert (first / "provider" / "repairs" / "001").is_dir()
    assert (second / "provider" / "repairs" / "001").is_dir()
    assert first != second
    assert [request.repair_cycle for request in provider.requests] == [0, 1, 0, 1]
    second_repair = next(
        request
        for request in provider.requests
        if request.item_id == "T002" and request.intent is not ChangeIntent.IMPLEMENT
    )
    assert second_repair.repair_cycle == 1


def test_repair_state_resets_before_second_item_failure(tmp_path) -> None:
    target = _multi_target(tmp_path, repair_validation=True)
    observed = []

    def observe(request, context):
        if request.item_id == "T002" and request.intent is ChangeIntent.IMPLEMENT:
            run = context.runtime_directory.parent.parent.parent
            from agentgraph.runtime import StateStore

            state = StateStore(run / "state.json").load()
            observed.append((state.repair.count, state.repair.history))

    report = _runner(
        target,
        tmp_path / "runtime",
        RepairingPerItemProvider(observer=observe),
        repairs=1,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    second_context = read_evidence(
        _item_roots(Path(report.runtime_path or ""))[1]
        / "operations"
        / "repairs"
        / "001"
        / "context.json"
    )["payload"]
    assert second_context["cycle"] == 1
    assert [item.repair_count for item in report.completed_items] == [1, 1]
    assert observed == [(0, ())]


def test_semantic_review_is_fresh_and_item_scoped_for_each_item(tmp_path) -> None:
    target = _multi_target(tmp_path)
    reviewer = SemanticReviewer(("pass", "pass"))

    report = _runner(
        target,
        tmp_path / "runtime",
        PerItemProvider(),
        reviewer=reviewer,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert len(reviewer.calls) == 2
    assert len({context.node_attempt_id for context in reviewer.contexts}) == 2
    roots = _item_roots(Path(report.runtime_path or ""))
    assert reviewer.contexts[0].runtime_directory.is_relative_to(roots[0])
    assert reviewer.contexts[1].runtime_directory.is_relative_to(roots[1])


def test_second_item_semantic_failure_repairs_then_gets_fresh_pass(tmp_path) -> None:
    target = _multi_target(tmp_path)
    reviewer = SemanticReviewer(("pass", "fail", "pass"))
    provider = RepairingPerItemProvider(clean_initial=True)

    report = _runner(
        target,
        tmp_path / "runtime",
        provider,
        repairs=1,
        reviewer=reviewer,
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert len(report.commit_shas) == 2
    assert len(reviewer.calls) == 3
    assert [item.repair_count for item in report.completed_items] == [0, 1]
    assert [request.item_id for request in provider.requests] == ["T001", "T002", "T002"]


def test_partial_terminal_failure_preserves_verified_first_item_report(tmp_path) -> None:
    target = _multi_target(tmp_path)
    provider = RepairingPerItemProvider(clean_initial=True, fail_item="T002")

    report = _runner(target, tmp_path / "runtime", provider).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.completed_item_ids == ("T001",)
    assert len(report.completed_items) == 1
    completed = report.completed_items[0]
    assert completed.item_id == "T001"
    assert completed.item_index == 1
    assert completed.item_base_head == report.baseline_head
    assert completed.commit_sha == report.commit_shas[0]
    assert completed.changed_paths == ("src/t001.py",)
    assert completed.repair_count == 0


def test_recomputed_item_inputs_tamper_requires_recovery(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    initial = _runner(target, runtime, PerItemProvider()).run(WriteSliceRequest(scope_id="E001"))
    path = _item_roots(Path(initial.runtime_path or ""))[0] / "write-inputs.json"

    def mutate(document):
        document["payload"]["base_branch"] = "malicious"

    _rewrite_evidence(path, mutate)
    resumed = _runner(target, runtime, PerItemProvider()).resume("run_m012")

    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "item_inputs_mismatch"


def test_completed_commit_receipt_substitution_cannot_replace_reviewed_witness(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    initial = _runner(target, runtime, PerItemProvider(), limit=1).run(
        WriteSliceRequest(scope_id="E001")
    )
    baseline = initial.baseline_head or ""
    tree = git(target, "rev-parse", f"{initial.commit_sha}^{{tree}}").strip().decode()
    malicious = (
        git(target, "commit-tree", tree, "-p", baseline, "-m", "substituted child").strip().decode()
    )
    run = Path(initial.runtime_path or "")
    commit_path = run / "operations" / "commit.json"

    def mutate(document):
        document["payload"]["commit_sha"] = malicious

    _rewrite_evidence(commit_path, mutate)
    git(target, "update-ref", "refs/heads/work/e001", malicious)
    git(run / "workspace", "reset", "--hard", malicious)

    resumed = _runner(target, runtime, PerItemProvider(), limit=1).resume("run_m012")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "multi_item_evidence_mismatch"


def test_cross_item_operation_evidence_substitution_fails_closed(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    initial = _runner(target, runtime, PerItemProvider()).run(WriteSliceRequest(scope_id="E001"))
    first, second = _item_roots(Path(initial.runtime_path or ""))
    shutil.rmtree(second / "operations")
    shutil.copytree(first / "operations", second / "operations")
    second_inputs = read_evidence(second / "write-inputs.json")
    for path in (second / "operations").rglob("*.json"):

        def mutate(document, values=second_inputs):
            for key in (
                "project_id",
                "run_id",
                "item_id",
                "item_index",
                "item_base_head",
                "target_baseline_head",
                "work_plan_digest",
                "source_revision",
                "capability_fingerprint",
            ):
                document[key] = values[key]
            document["scope_id"] = "E001"

        _rewrite_evidence(path, mutate)

    resumed = _runner(target, runtime, PerItemProvider()).resume("run_m012")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "multi_item_evidence_mismatch"


@pytest.mark.parametrize("kind", ("items", "item_root", "write_inputs", "operations"))
def test_item_storage_links_are_rejected_without_touching_external_files(tmp_path, kind) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    initial = _runner(target, runtime, PerItemProvider()).run(WriteSliceRequest(scope_id="E001"))
    run = Path(initial.runtime_path or "")
    first = _item_roots(run)[0]
    external = tmp_path / f"external-{kind}"
    sentinel = external / "sentinel.txt" if kind != "write_inputs" else external
    if kind == "write_inputs":
        sentinel.write_text("unchanged", encoding="utf-8")
        (first / "write-inputs.json").unlink()
        _symlink(first / "write-inputs.json", external, directory=False)
    else:
        external.mkdir()
        sentinel.write_text("unchanged", encoding="utf-8")
        if kind == "items":
            shutil.rmtree(run / "items")
            _symlink(run / "items", external, directory=True)
        elif kind == "item_root":
            shutil.rmtree(first)
            _symlink(first, external, directory=True)
        else:
            shutil.rmtree(first / "operations")
            _symlink(first / "operations", external, directory=True)

    resumed = _runner(target, runtime, PerItemProvider()).resume("run_m012")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "item_evidence_invalid"
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def _crash_between_items(tmp_path: Path, *, reverse_dependency: bool = False):
    target = _multi_target(tmp_path, reverse_dependency=reverse_dependency)
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    runner = _runner(
        target,
        runtime,
        provider,
        fault=StageFault("after_transition_committed", 12),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    state = runner._coordinator  # preserve an explicit assertion below via resumed public state
    assert state is not None
    expected_first = "T002" if reverse_dependency else "T001"
    assert provider.item_ids == [expected_first]
    return target, runtime, provider


def test_crash_after_first_more_work_resumes_without_duplicate_commit_or_provider(tmp_path) -> None:
    target, runtime, provider = _crash_between_items(tmp_path)
    first_commit = git(target, "rev-parse", "work/e001").strip().decode()

    resumed = _runner(target, runtime, provider).resume("run_m012")

    assert resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert provider.item_ids == ["T001", "T002"]
    assert resumed.commit_shas[0] == first_commit
    assert len(resumed.commit_shas) == 2


def test_reverse_dependency_crash_after_more_work_resumes_without_duplicate_item(tmp_path) -> None:
    target, runtime, provider = _crash_between_items(tmp_path, reverse_dependency=True)
    first_commit = git(target, "rev-parse", "work/e001").strip().decode()

    resumed = _runner(target, runtime, provider).resume("run_m012")

    assert resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert provider.item_ids == ["T002", "T001"]
    assert resumed.completed_item_ids == ("T002", "T001")
    assert resumed.commit_shas[0] == first_commit
    assert git(target, "rev-parse", f"{resumed.commit_shas[0]}^").strip().decode() == (
        resumed.baseline_head
    )
    assert (
        git(target, "rev-parse", f"{resumed.commit_shas[1]}^").strip().decode()
        == (resumed.commit_shas[0])
    )


def test_reverse_dependency_forged_first_completion_fails_selection_replay(tmp_path) -> None:
    target, runtime, provider = _crash_between_items(tmp_path, reverse_dependency=True)
    run = RuntimePaths.resolve(runtime).run("prj_m012", "run_m012")

    def forge(state):
        claimed = replace(state.work.completed_items[0], id="T001")
        return replace(state, work=replace(state.work, completed_items=(claimed,)))

    _rewrite_durable_state(run, forge)
    resumed = _runner(target, runtime, provider).resume("run_m012")

    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "multi_item_lineage_mismatch"
    assert provider.item_ids == ["T002"]


def test_crash_after_second_select_resumes_same_item_and_baseline(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    runner = _runner(
        target,
        runtime,
        provider,
        fault=StageFault("after_transition_committed", 13),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    first_commit = git(target, "rev-parse", "work/e001").strip().decode()

    resumed = _runner(target, runtime, provider).resume("run_m012")
    assert resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert provider.item_ids == ["T001", "T002"]
    assert provider.baselines == [resumed.baseline_head, first_commit]


def test_resume_rejects_current_item_that_does_not_match_next_ready_selection(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    runner = _runner(
        target,
        runtime,
        provider,
        fault=StageFault("after_transition_committed", 4),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    run = RuntimePaths.resolve(runtime).run("prj_m012", "run_m012")

    def forge(state):
        assert state.work.item is not None
        return replace(state, work=replace(state.work, item=replace(state.work.item, id="T002")))

    _rewrite_durable_state(run, forge)
    resumed = _runner(target, runtime, provider).resume("run_m012")

    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "multi_item_lineage_mismatch"
    assert provider.item_ids == []


def test_interrupted_second_explore_reruns_with_fresh_item_evidence(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    agent = CountingAgent()
    runner = _runner(
        target,
        runtime,
        provider,
        agent=agent,
        fault=StageFault("after_node_invocation", 14),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    before = agent.calls.count("explore")

    resumed = _runner(target, runtime, provider, agent=agent).resume("run_m012")
    assert resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert before == 2
    assert agent.calls.count("explore") == 3
    second = _item_roots(Path(resumed.runtime_path or ""))[1]
    evidence = second / "provider" / agent.evidence_namespace / "agents" / "EXPLORE"
    attempts = tuple(evidence.iterdir())
    assert len(attempts) == 2


def test_interrupted_second_implement_is_unreconciled_and_never_reinvoked(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    runner = _runner(
        target,
        runtime,
        provider,
        fault=StageFault("after_node_started", 17),
    )
    with pytest.raises(RuntimeError, match="M012 interruption"):
        runner.run(WriteSliceRequest(scope_id="E001"))
    assert provider.item_ids == ["T001"]

    resumed = _runner(target, runtime, provider).resume("run_m012")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "unreconciled_side_effect_capability"
    assert provider.item_ids == ["T001"]
    assert resumed.completed_item_ids == ("T001",)


@pytest.mark.parametrize("drift", ("source", "target", "workspace", "scope_ref"))
def test_drift_between_items_fails_recovery_before_second_provider(tmp_path, drift) -> None:
    target, runtime, provider = _crash_between_items(tmp_path)
    run = RuntimePaths.resolve(runtime).run("prj_m012", "run_m012")
    if drift == "source":
        tasks = target / "specs" / "one" / "tasks.md"
        tasks.write_text(tasks.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif drift == "target":
        (target / "tracked.txt").write_text("drift\n", encoding="utf-8")
        git(target, "add", "--", "tracked.txt")
        git(target, "commit", "--quiet", "-m", "target drift")
    elif drift == "workspace":
        (run / "workspace" / "untracked.txt").write_text("drift\n", encoding="utf-8")
    else:
        git(target, "update-ref", "refs/heads/work/e001", "HEAD")

    resumed = _runner(target, runtime, provider).resume("run_m012")
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert provider.item_ids == ["T001"]


@pytest.mark.parametrize("decision", (CheckpointOutcome.APPROVED, CheckpointOutcome.REJECTED))
def test_critical_second_item_checkpoint_preserves_first_commit(tmp_path, decision) -> None:
    target = _multi_target(tmp_path, critical_item="T002")
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    runner = _runner(target, runtime, provider)

    paused = runner.run(WriteSliceRequest(scope_id="E001"))
    assert paused.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert paused.completed_item_ids == ("T001",)
    assert provider.item_ids == ["T001"]
    first_commit = paused.commit_shas[0]
    workspace = Path(paused.workspace_path or "")
    assert git(workspace, "status", "--porcelain") == b""
    assert git(target, "rev-parse", "work/e001").strip().decode() == first_commit
    assert paused.checkpoint is not None
    request_path = next(Path(paused.runtime_path or "").glob("checkpoints/*/request.json"))
    checkpoint_request = json.loads(request_path.read_text(encoding="utf-8"))
    assert checkpoint_request["baseline_head"] == first_commit
    assert checkpoint_request["baseline_tree_id"] == (
        git(target, "rev-parse", f"{first_commit}^{{tree}}").strip().decode()
    )
    runner.submit_checkpoint(
        "run_m012",
        checkpoint_id=paused.checkpoint.checkpoint_id,
        nonce=paused.checkpoint.nonce,
        outcome=decision,
        actor="m012-operator",
    )

    resumed = runner.resume("run_m012")
    if decision is CheckpointOutcome.APPROVED:
        assert resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
        assert provider.item_ids == ["T001", "T002"]
        assert len(resumed.commit_shas) == 2
    else:
        assert resumed.outcome is WriteSliceOutcome.BLOCKED
        assert provider.item_ids == ["T001"]
        assert resumed.completed_item_ids == ("T001",)
        assert git(target, "rev-parse", "work/e001").strip().decode() == first_commit


def test_delivery_boundary_resets_review_and_repeated_resume_is_idempotent(tmp_path) -> None:
    target = _multi_target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = PerItemProvider()
    runner = _runner(target, runtime, provider)
    initial = runner.run(WriteSliceRequest(scope_id="E001"))

    assert initial.graph_state is not None
    assert initial.graph_state.graph.current_node == "DELIVERY_REVIEW"
    assert initial.graph_state.run.status is RunStatus.RUNNING
    assert initial.graph_state.review.verdict is ReviewVerdict.UNKNOWN
    assert not (Path(initial.runtime_path or "") / "final.json").exists()
    calls = tuple(provider.item_ids)
    commits = initial.commit_shas
    first = runner.resume("run_m012")
    second = runner.resume("run_m012")
    assert first.outcome is second.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert first.commit_shas == second.commit_shas == commits
    assert tuple(provider.item_ids) == calls
