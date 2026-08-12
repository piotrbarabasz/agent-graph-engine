from __future__ import annotations

import shutil

import pytest

from agentgraph.infra import GitAdapter, ProcessRunner
from agentgraph.runtime import Journal, JournalRecordType, RecoveryAction
from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.write import PostCommitRecoveryRequired, WriteSliceOutcome, WriteSliceRequest
from agentgraph.write.evidence import read_evidence
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


class TransitionFault:
    def __init__(self, target: int):
        self.target = target
        self.count = 0

    def __call__(self, stage: str) -> None:
        if stage == "after_transition_committed":
            self.count += 1
            if self.count == self.target:
                raise RuntimeError("injected clean process stop")


class CountingProvider:
    def __init__(self):
        self.count = 0

    def propose(self, request):
        from agentgraph.write import ChangeSet, FileChange

        self.count += 1
        return ChangeSet.create((FileChange("src/t001.py", None, "value = 1\n"),))


class CountingProcessRunner:
    def __init__(self):
        self.delegate = ProcessRunner()
        self.count = 0

    def run(self, spec, *, cancellation=None):
        self.count += 1
        return self.delegate.run(spec, cancellation=cancellation)


class FailPostCommitSnapshotAdapter(GitAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_workspace_snapshot = False

    def commit(self, *args, **kwargs):
        result = super().commit(*args, **kwargs)
        self.fail_workspace_snapshot = True
        return result

    def snapshot(self, repository):
        if self.fail_workspace_snapshot and repository.root.name == "workspace":
            self.fail_workspace_snapshot = False
            raise RuntimeError("injected post-commit verification failure")
        return super().snapshot(repository)


class CommitThenRaiseAdapter(GitAdapter):
    def commit(self, *args, **kwargs):
        super().commit(*args, **kwargs)
        raise RuntimeError("injected failure after commit command")


class CommitMutationAdapter(GitAdapter):
    def __init__(self, *args, mutation: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.mutation = mutation

    def commit(self, repository, message, *, expected_paths=None, identity=None):
        del expected_paths
        if self.mutation == "additional":
            candidate = repository.root / "README.md"
            candidate.write_text("commit-time addition\n", encoding="utf-8")
            self.stage_paths(repository, ("README.md",))
        else:
            candidate = repository.root / "src" / "t001.py"
            candidate.write_text("value = 'mutated during commit'\n", encoding="utf-8")
            self.stage_paths(repository, ("src/t001.py",))
        return super().commit(repository, message, expected_paths=None, identity=identity)


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
    restarted = _runner(target, tmp_path / "runtime")
    report = restarted.resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED


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
    restarted = _runner(target, tmp_path / "runtime")
    report = restarted.resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.commit_sha == branch_head.decode()
    assessment = runner.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.BLOCKED
    assert assessment.resume_node == "CLOSE_TASK"
    assert git(target, "rev-parse", "refs/heads/work/e001").strip() == branch_head
    assert git(target, "rev-list", "--count", f"{base.decode()}..work/e001").strip() == b"1"


@pytest.mark.parametrize(
    ("transition_count", "first_resumed_node"),
    ((8, "VALIDATE"), (9, "REVIEW"), (10, "CLOSE_TASK")),
)
def test_new_runner_clean_resumes_committed_transition_without_repeating_effects(
    tmp_path, transition_count, first_resumed_node
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = CountingProvider()
    processes = CountingProcessRunner()
    first = _runner(
        target,
        runtime,
        provider,
        process_runner=processes,
        fault=TransitionFault(transition_count),
    )
    with pytest.raises(RuntimeError, match="clean process stop"):
        first.run(WriteSliceRequest(scope_id="E001"))
    validation_count = processes.count
    run_path = first.paths.run("prj_m006_fixture", "run_m006_fixture")
    assert (run_path / "write-inputs.json").is_file()

    restarted = _runner(target, runtime, provider, process_runner=processes)
    assessment = restarted.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.CLEAN_RESUME
    assert assessment.resume_node == first_resumed_node
    report = restarted.resume("run_m006_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes[0] == first_resumed_node
    assert provider.count == 1
    if first_resumed_node in {"REVIEW", "CLOSE_TASK"}:
        assert processes.count == validation_count
    base = report.base_head
    assert base is not None
    assert git(target, "rev-list", "--count", f"{base}..work/e001").strip() == b"1"


@pytest.mark.parametrize(
    ("invocation_count", "resume_node"),
    ((9, "VALIDATE"), (10, "REVIEW")),
)
def test_safe_interrupted_node_reuses_finalized_evidence_on_new_runner(
    tmp_path, invocation_count, resume_node
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = CountingProvider()
    processes = CountingProcessRunner()
    first = _runner(
        target,
        runtime,
        provider,
        process_runner=processes,
        fault=InvocationFault(invocation_count),
    )
    with pytest.raises(RuntimeError, match="interruption"):
        first.run(WriteSliceRequest(scope_id="E001"))
    validation_count = processes.count

    restarted = _runner(target, runtime, provider, process_runner=processes)
    assessment = restarted.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.RERUN_INTERRUPTED_NODE
    assert assessment.resume_node == resume_node
    report = restarted.resume("run_m006_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes[0] == resume_node
    assert provider.count == 1
    assert processes.count == validation_count


def test_post_commit_verification_uncertainty_leaves_witness_and_blocks_recovery(
    tmp_path,
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    base = git(target, "rev-parse", "HEAD").strip()
    failing_git = FailPostCommitSnapshotAdapter(executable=shutil.which("git") or "git")
    runner = _runner(target, runtime, git_adapter=failing_git)

    with pytest.raises(PostCommitRecoveryRequired):
        runner.run(WriteSliceRequest(scope_id="E001"))

    branch_head = git(target, "rev-parse", "refs/heads/work/e001").strip()
    assert branch_head != base
    run_path = runner.paths.run("prj_m006_fixture", "run_m006_fixture")
    witness_path = run_path / "operations" / "commit-witness.json"
    assert witness_path.is_file()
    witness = read_evidence(witness_path)["payload"]
    assert witness["project_id"] == "prj_m006_fixture"
    assert witness["run_id"] == "run_m006_fixture"
    assert witness["item_id"] == "T001"
    assert witness["scope_id"] == "E001"
    assert witness["base_head"] == base.decode()
    assert witness["previous_branch_head"] == base.decode()
    assert witness["commit_sha"] == branch_head.decode()
    assert witness["reviewed_paths"] == ["src/t001.py"]
    assert not (run_path / "operations" / "commit.json").exists()
    records = Journal(run_path / "journal.jsonl", "run_m006_fixture").load()
    assert records[-1].record_type is JournalRecordType.NODE_STARTED
    assert records[-1].payload["node_id"] == "CLOSE_TASK"
    restarted = _runner(target, runtime)
    report = restarted.resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.commit_sha == branch_head.decode()
    assert report.graph_state is not None
    assert report.graph_state.graph.current_node == "CLOSE_TASK"
    assert git(target, "rev-list", "--count", f"{base.decode()}..work/e001").strip() == b"1"
    second = restarted.resume("run_m006_fixture")
    assert second.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert git(target, "rev-list", "--count", f"{base.decode()}..work/e001").strip() == b"1"


def test_git_adapter_exception_after_real_commit_is_treated_as_uncertain(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    base = git(target, "rev-parse", "HEAD").strip()
    adapter = CommitThenRaiseAdapter(executable=shutil.which("git") or "git")
    runner = _runner(target, runtime, git_adapter=adapter)

    with pytest.raises(PostCommitRecoveryRequired):
        runner.run(WriteSliceRequest(scope_id="E001"))

    branch_head = git(target, "rev-parse", "refs/heads/work/e001").strip()
    assert branch_head != base
    witness = (
        runner.paths.run("prj_m006_fixture", "run_m006_fixture")
        / "operations"
        / "commit-witness.json"
    )
    assert witness.is_file()
    report = _runner(target, runtime).resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.commit_sha == branch_head.decode()
    assert git(target, "rev-list", "--count", f"{base.decode()}..work/e001").strip() == b"1"


def test_new_runner_returns_completed_run_without_reinvoking_provider(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = CountingProvider()
    first = _runner(target, runtime, provider).run(WriteSliceRequest(scope_id="E001"))
    assert first.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED

    restarted = _runner(target, runtime, provider)
    report = restarted.resume("run_m006_fixture")

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.commit_sha == first.commit_sha
    assert report.executed_nodes == ()
    assert provider.count == 1


@pytest.mark.parametrize("mutation", ("additional", "content"))
def test_commit_time_tree_mutation_is_post_commit_recovery_required(tmp_path, mutation) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    base = git(target, "rev-parse", "HEAD").strip()
    adapter = CommitMutationAdapter(executable=shutil.which("git") or "git", mutation=mutation)
    runner = _runner(target, runtime, git_adapter=adapter)

    with pytest.raises(PostCommitRecoveryRequired):
        runner.run(WriteSliceRequest(scope_id="E001"))

    branch_head = git(target, "rev-parse", "refs/heads/work/e001").strip()
    assert branch_head != base
    committed = git(
        target, "diff-tree", "--no-commit-id", "--name-only", "-r", branch_head.decode()
    ).splitlines()
    if mutation == "additional":
        assert set(committed) == {b"README.md", b"src/t001.py"}
    else:
        assert committed == [b"src/t001.py"]
        blob = git(target, "show", f"{branch_head.decode()}:src/t001.py")
        assert blob == b"value = 'mutated during commit'\n"
    run_path = runner.paths.run("prj_m006_fixture", "run_m006_fixture")
    assert (run_path / "operations" / "commit-witness.json").is_file()
    assert not (run_path / "operations" / "commit.json").exists()
    records = Journal(run_path / "journal.jsonl", "run_m006_fixture").load()
    assert records[-1].record_type is JournalRecordType.NODE_STARTED
    assert records[-1].payload["node_id"] == "CLOSE_TASK"
    report = _runner(target, runtime).resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert git(target, "rev-list", "--count", f"{base.decode()}..work/e001").strip() == b"1"


def test_write_inputs_exist_before_activation_and_immediate_restart_can_resume(
    tmp_path,
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    provider = CountingProvider()
    first = _runner(
        target,
        runtime,
        provider,
        fault=lambda stage: (
            (_ for _ in ()).throw(RuntimeError("activation stop"))
            if stage == "after_run_activation"
            else None
        ),
    )

    with pytest.raises(RuntimeError, match="activation stop"):
        first.run(WriteSliceRequest(scope_id="E001"))

    run_path = first.paths.run("prj_m006_fixture", "run_m006_fixture")
    assert (run_path / "write-inputs.json").is_file()
    assert first.paths.active_run("prj_m006_fixture").is_file()
    restarted = _runner(target, runtime, provider)
    assessment = restarted.assess_recovery("run_m006_fixture")
    assert assessment.action is RecoveryAction.CLEAN_RESUME
    assert assessment.resume_node == "START"
    report = restarted.resume("run_m006_fixture")
    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert provider.count == 1


@pytest.mark.parametrize(
    ("crash_stage", "promoted"),
    [
        ("after_run_started", False),
        ("before_run_promotion", False),
        ("after_run_promotion", True),
        ("before_run_activation", True),
        ("after_run_activation", True),
    ],
)
def test_m006_initialization_fault_never_promotes_without_valid_write_inputs(
    tmp_path, crash_stage: str, promoted: bool
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"

    def fault(stage: str) -> None:
        if stage == crash_stage:
            raise RuntimeError("initialization boundary stop")

    runner = _runner(target, runtime, fault=fault)
    with pytest.raises(RuntimeError, match="boundary"):
        runner.run(WriteSliceRequest(scope_id="E001"))

    run_path = runner.paths.run("prj_m006_fixture", "run_m006_fixture")
    assert run_path.exists() is promoted
    if promoted:
        evidence = read_evidence(run_path / "write-inputs.json")
        assert evidence["project_id"] == "prj_m006_fixture"
        assert evidence["run_id"] == "run_m006_fixture"
        assert evidence["item_id"] == "T001"
        assert evidence["scope_id"] == "E001"
    else:
        assert not runner.paths.active_run("prj_m006_fixture").exists()
    assert git(target, "status", "--porcelain") == b""
    assert git(target, "branch", "--list", "work/e001") == b""


def test_write_inputs_failure_preserves_only_incomplete_initialization(
    tmp_path, monkeypatch
) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"

    def fail(path, **_kwargs) -> None:
        atomic_write_bytes(path.parent / "partial-write-inputs", b"diagnostic")
        raise OSError("write inputs failed")

    monkeypatch.setattr("agentgraph.write.runner.write_evidence", fail)
    runner = _runner(target, runtime)
    with pytest.raises(OSError, match="write inputs"):
        runner.run(WriteSliceRequest(scope_id="E001"))

    staging = runner.paths.initializing_run("prj_m006_fixture", "run_m006_fixture")
    assert staging.is_dir()
    assert (staging / "partial-write-inputs").read_bytes() == b"diagnostic"
    assert not runner.paths.run("prj_m006_fixture", "run_m006_fixture").exists()
    assert not runner.paths.active_run("prj_m006_fixture").exists()
    assert git(target, "status", "--porcelain") == b""
    assert git(target, "branch", "--list", "work/e001") == b""


def test_resume_fails_closed_when_operation_evidence_digest_is_corrupt(tmp_path) -> None:
    target = _target(tmp_path)
    runtime = tmp_path / "runtime"
    first = _runner(target, runtime, fault=TransitionFault(8))
    with pytest.raises(RuntimeError, match="clean process stop"):
        first.run(WriteSliceRequest(scope_id="E001"))
    evidence = (
        first.paths.run("prj_m006_fixture", "run_m006_fixture")
        / "operations"
        / "implement-applied.json"
    )
    raw = evidence.read_text(encoding="utf-8")
    evidence.write_text(
        raw.replace('"content_digest":"sha256:', '"content_digest":"sha256:0'), encoding="utf-8"
    )

    report = _runner(target, runtime).resume("run_m006_fixture")

    assert report.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert report.issues[0].code == "write_rehydration_mismatch"
