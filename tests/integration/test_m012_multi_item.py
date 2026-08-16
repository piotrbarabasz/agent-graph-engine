from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.core import RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.runtime.codec import decode_value
from agentgraph.write import ChangeSet, FileChange, WriteSliceOutcome, WriteSliceRequest
from agentgraph.write.evidence import read_evidence
from agentgraph.write.models import WriteInputs, WriteRunInputs
from agentgraph.write.runner import WriteSliceRunner

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


def _multi_target(tmp_path: Path, *, dependency: bool = False) -> Path:
    target = tmp_path / "target"
    initialize_target(target)
    manifest = target / ".specify" / "workstreams" / "E001.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "tasks:\n  - T001", "tasks:\n  - T001\n  - T002"
        ),
        encoding="utf-8",
    )
    tasks = target / "specs" / "one" / "tasks.md"
    safe = "python -c \"print('ok')\""
    first = task_block("T001", owner="E001").replace(
        "python -c \"from pathlib import Path; Path('executed.txt').write_text('bad')\"",
        safe,
    )
    second = task_block(
        "T002", owner="E001", dependencies="T001" if dependency else "None"
    ).replace(
        "python -c \"from pathlib import Path; Path('executed.txt').write_text('bad')\"",
        safe,
    )
    tasks.write_text(f"{first}\n{second}", encoding="utf-8")
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "two item scope")
    return target


def _runner(target: Path, runtime: Path, provider, *, limit: int = 3) -> WriteSliceRunner:
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    paths = RuntimePaths.resolve(runtime)
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        provider,
        git_adapter=adapter,
        project_registry=ProjectRegistry(paths, project_id_factory=lambda: "prj_m012"),
        commit_identity=GitCommitIdentity("M012 Test", "m012@example.test"),
        run_id_factory=lambda: "run_m012",
        max_work_items_per_run=limit,
    )


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
