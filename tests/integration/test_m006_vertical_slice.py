from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.core import RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.work import RepoPathSpec, WorkPackage
from agentgraph.write import (
    ChangeSet,
    FileChange,
    WriteSliceOutcome,
    WriteSliceRequest,
    WriteSliceRunner,
)

from .conftest import git, initialize_target, semantic_git_state, working_tree_bytes


class NewFileProvider:
    def propose(self, request):
        assert not hasattr(request, "workspace_path")
        return ChangeSet.create((FileChange("src/t001.py", None, "value = 1\n"),))


class OutOfScopeProvider:
    def propose(self, request):
        return ChangeSet.create((FileChange("README.md", None, "escalation\n"),))


class ExistingScriptProvider:
    def __init__(self, before: str):
        self.before = before

    def propose(self, request):
        return ChangeSet.create(
            (FileChange("scripts/run.sh", self.before, "#!/bin/sh\necho changed\n"),)
        )


def _runner(
    target: Path,
    runtime: Path,
    provider=None,
    *,
    git_adapter=None,
    process_runner=None,
    fault=None,
) -> WriteSliceRunner:
    executable = shutil.which("git") or "git"
    adapter = git_adapter or GitAdapter(executable=executable)
    paths = RuntimePaths.resolve(runtime)
    registry = ProjectRegistry(paths, project_id_factory=lambda: "prj_m006_fixture")
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        provider or NewFileProvider(),
        git_adapter=adapter,
        project_registry=registry,
        process_runner=process_runner,
        commit_identity=GitCommitIdentity("M006 Test", "m006@example.test"),
        run_id_factory=lambda: "run_m006_fixture",
        fault=fault,
    )


def _target(tmp_path: Path, command: str = "python -c \"print('ok')\"") -> Path:
    target = tmp_path / "target"
    initialize_target(target)
    tasks = target / "specs" / "one" / "tasks.md"
    text = tasks.read_text(encoding="utf-8").replace(
        "python -c \"from pathlib import Path; Path('executed.txt').write_text('bad')\"",
        command,
    )
    tasks.write_text(text, encoding="utf-8")
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "safe validation")
    return target


def test_happy_path_creates_one_local_commit_and_preserves_target(tmp_path) -> None:
    target = _target(tmp_path)
    before_git = semantic_git_state(target)
    before_bytes = working_tree_bytes(target)
    before_source = (target / "specs" / "one" / "tasks.md").read_bytes()

    report = _runner(target, tmp_path / "runtime").run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.PAUSED
    assert report.graph_state.graph.current_node == "END"
    assert report.commit_sha is not None
    assert report.changeset_digest is not None
    assert report.changed_paths == ("src/t001.py",)
    assert report.item_id == "T001"
    assert report.scope_id == "E001"
    assert report.base_head == before_git[0].strip().decode()
    assert report.runtime_reference == "prj_m006_fixture/run_m006_fixture"
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
        "REVIEW",
        "CLOSE_TASK",
        "MORE_WORK",
        "FINALIZE",
    )
    assert semantic_git_state(target) == before_git
    assert working_tree_bytes(target) == before_bytes
    assert (target / "specs" / "one" / "tasks.md").read_bytes() == before_source
    assert git(target, "rev-parse", "refs/heads/work/e001").strip().decode() == report.commit_sha
    assert git(
        target, "diff-tree", "--no-commit-id", "--name-only", "-r", report.commit_sha
    ).splitlines() == [b"src/t001.py"]
    workspace = Path(report.workspace_path or "")
    assert workspace.is_dir()
    assert workspace.is_relative_to(Path(report.runtime_path or ""))
    assert not workspace.is_relative_to(target)
    assert git(workspace, "status", "--porcelain") == b""
    assert not (Path(report.runtime_path or "").parents[1] / "active-run.json").exists()
    for name in (
        "implement-proposal.json",
        "implement-applied.json",
        "validation.json",
        "review.json",
        "commit.json",
    ):
        assert (Path(report.runtime_path or "") / "operations" / name).is_file()


def test_existing_scope_branch_blocks_before_durable_run(tmp_path) -> None:
    target = _target(tmp_path)
    git(target, "branch", "work/e001")

    report = _runner(target, tmp_path / "runtime").run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.run_id is None
    assert report.issues[0].code == "scope_branch_already_exists"


def test_repository_root_mismatch_returns_typed_early_report(tmp_path) -> None:
    target = _target(tmp_path)

    report = _runner(target / "specs", tmp_path / "runtime").run()

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.run_id is None
    assert report.issues[0].code == "repository_root_mismatch"


def test_active_scope_and_critical_item_are_blocked_without_worktree(tmp_path) -> None:
    active_root = tmp_path / "active"
    active_root.mkdir()
    active = active_root / "target"
    initialize_target(active, status="active", active_scope="E001")
    active_report = _runner(active, active_root / "runtime").run(WriteSliceRequest(scope_id="E001"))
    assert active_report.outcome is WriteSliceOutcome.BLOCKED
    assert active_report.run_id is None
    assert active_report.issues[0].code == "active_scope_write_not_supported_in_m006"

    critical_root = tmp_path / "critical"
    critical_root.mkdir()
    critical = _target(critical_root)
    tasks = critical / "specs" / "one" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace("Risk: medium", "Risk: critical"),
        encoding="utf-8",
    )
    git(critical, "add", "--all")
    git(critical, "commit", "--quiet", "-m", "critical risk")
    critical_report = _runner(critical, critical_root / "runtime").run(
        WriteSliceRequest(scope_id="E001")
    )
    assert critical_report.outcome is WriteSliceOutcome.BLOCKED
    assert critical_report.run_id is None
    assert critical_report.issues[0].code == "critical_risk_not_supported_in_m006"


def test_out_of_scope_proposal_fails_without_commit(tmp_path) -> None:
    target = _target(tmp_path)
    base = git(target, "rev-parse", "HEAD").strip()

    report = _runner(target, tmp_path / "runtime", OutOfScopeProvider()).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert git(target, "rev-parse", "refs/heads/work/e001").strip() == base
    assert git(target, "status", "--porcelain") == b""


def test_validation_failure_preserves_dirty_workspace_and_does_not_commit(tmp_path) -> None:
    target = _target(tmp_path, 'python -c "raise SystemExit(7)"')
    base = git(target, "rev-parse", "HEAD").strip()

    report = _runner(target, tmp_path / "runtime").run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.graph_state is not None
    assert report.graph_state.run.status is RunStatus.FAILED
    assert report.graph_state.failure.code == "validation_failed"
    assert git(target, "rev-parse", "refs/heads/work/e001").strip() == base
    assert git(Path(report.workspace_path or ""), "status", "--porcelain")


def test_validation_side_effect_is_rejected_by_review(tmp_path) -> None:
    command = "python -c \"from pathlib import Path; Path('unexpected.tmp').write_text('x')\""
    target = _target(tmp_path, command)
    base = git(target, "rev-parse", "HEAD").strip()

    report = _runner(target, tmp_path / "runtime").run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.graph_state is not None
    assert "changed_paths_mismatch" in report.graph_state.review.findings
    assert git(target, "rev-parse", "refs/heads/work/e001").strip() == base


class PackageEscalatingSource:
    def __init__(self, source, *, branch: bool = False):
        self.source = source
        self.branch = branch

    def __getattr__(self, name):
        return getattr(self.source, name)

    def build_package(self, snapshot, item_id) -> WorkPackage:
        package = self.source.build_package(snapshot, item_id)
        if self.branch:
            return replace(package, branch_hint="evil/branch")
        return replace(package, allowed_paths=(*package.allowed_paths, RepoPathSpec("README.md")))


def test_package_allowlist_and_branch_escalation_fail_before_durable_run(tmp_path) -> None:
    for index, branch in enumerate((False, True)):
        case = tmp_path / str(index)
        case.mkdir()
        target = _target(case)
        source = PackageEscalatingSource(SpecKitAdapter(SpecKitLayout(target)), branch=branch)
        executable = shutil.which("git") or "git"
        adapter = GitAdapter(executable=executable)
        paths = RuntimePaths.resolve(tmp_path / str(index) / "runtime")
        runner = WriteSliceRunner(
            target,
            source,
            NewFileProvider(),
            git_adapter=adapter,
            project_registry=ProjectRegistry(
                paths, project_id_factory=lambda index=index: f"prj_m006_{index}"
            ),
        )

        report = runner.run(WriteSliceRequest(scope_id="E001"))

        assert report.outcome is WriteSliceOutcome.INVALID_SOURCE
        assert report.run_id is None
        assert report.issues[0].code == "work_package_capability_mismatch"
        assert not git(target, "branch", "--list", "work/e001").strip()


def _executable_target(tmp_path: Path, validation: str) -> tuple[Path, str]:
    target = _target(tmp_path, validation)
    script = target / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    tasks = target / "specs" / "one" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace(
            "Implementation files: `src/t001.py`",
            "Implementation files: `scripts/run.sh`",
        ),
        encoding="utf-8",
    )
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "executable fixture")
    return target, hashlib.sha256(script.read_bytes()).hexdigest()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit semantics are unavailable")
def test_content_only_commit_preserves_executable_mode(tmp_path) -> None:
    target, before = _executable_target(tmp_path, "python -c \"print('ok')\"")
    report = _runner(target, tmp_path / "runtime", ExistingScriptProvider(before)).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.commit_sha is not None
    tree = git(target, "ls-tree", report.commit_sha, "scripts/run.sh").split()
    assert tree[0] == b"100755"
    summary = git(target, "diff-tree", "--summary", f"{report.base_head}..{report.commit_sha}")
    assert b"mode change" not in summary


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit semantics are unavailable")
def test_validation_mode_change_fails_deterministic_review(tmp_path) -> None:
    command = "python -c \"import os; os.chmod('scripts/run.sh', 0o644)\""
    target, before = _executable_target(tmp_path, command)
    report = _runner(target, tmp_path / "runtime", ExistingScriptProvider(before)).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.graph_state is not None
    assert "final_mode_mismatch:scripts/run.sh" in report.graph_state.review.findings
