"""Stateful execution capability owned by one durable M006 run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from agentgraph.infra import (
    CommandReceipt,
    CommandSpec,
    DiffCheckResult,
    GitAdapter,
    GitCommitIdentity,
    GitRepository,
    ProcessRunner,
    ProcessStatus,
)
from agentgraph.integration import ShadowInputs, verify_work_source_revision
from agentgraph.work import WorkSource

from .apply import apply_changeset
from .errors import (
    CommitVerificationError,
    ValidationExecutionError,
    WorkspaceError,
    WriteBaselineDriftError,
)
from .evidence import write_evidence
from .models import AppliedChangeSet, ChangeRequest, ChangeSet, WriteInputs
from .provider import ChangeProvider


@dataclass(slots=True)
class WriteExecution:
    """The only object holding workspace, process, and Git write capabilities."""

    shadow: ShadowInputs
    inputs: WriteInputs
    source: WorkSource
    provider: ChangeProvider
    git: GitAdapter
    processes: ProcessRunner
    target: GitRepository
    run_id: str
    run_path: Path
    commit_identity: GitCommitIdentity
    validation_timeout_seconds: float = 120.0
    workspace: Path = field(init=False)
    operations: Path = field(init=False)
    workspace_repository: GitRepository | None = field(default=None, init=False)
    changeset: ChangeSet | None = field(default=None, init=False)
    applied: AppliedChangeSet | None = field(default=None, init=False)
    validation_receipts: tuple[CommandReceipt, ...] = field(default=(), init=False)
    diff_check: DiffCheckResult | None = field(default=None, init=False)
    validation_passed: bool = field(default=False, init=False)
    review_passed: bool = field(default=False, init=False)
    review_findings: tuple[str, ...] = field(default=(), init=False)
    commit_sha: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.workspace = self.run_path / "workspace"
        self.operations = self.run_path / "operations"
        run_root = self.run_path.resolve()
        candidate = self.workspace.resolve()
        try:
            candidate.relative_to(run_root)
        except ValueError as exc:
            raise WorkspaceError("workspace escapes durable run directory") from exc
        target = self.target.root.resolve()
        try:
            candidate.relative_to(target)
        except ValueError:
            pass
        else:
            raise WorkspaceError("workspace must be outside target repository")
        if self.workspace.exists() or self.workspace.is_symlink():
            raise WorkspaceError("workspace path already exists")

    def implement(self) -> AppliedChangeSet:
        self.live_recheck("write_baseline_drift")
        if self.git.local_branch_exists(self.target, self.inputs.scope_branch):
            raise WriteBaselineDriftError("scope_branch_already_exists")
        result = self.git.add_worktree(
            self.target,
            self.workspace,
            self.inputs.scope_branch,
            self.inputs.baseline_head,
        )
        self.workspace_repository = result.repository
        workspace_snapshot = self.git.snapshot(result.repository)
        if (
            workspace_snapshot.head_sha != self.inputs.baseline_head
            or workspace_snapshot.branch != self.inputs.scope_branch
            or workspace_snapshot.dirty
        ):
            raise WorkspaceError("new worktree does not match pinned baseline")
        request = self.change_request()
        proposed = self.provider.propose(request)
        if not isinstance(proposed, ChangeSet):
            raise WorkspaceError("ChangeProvider returned a non-ChangeSet value")
        self.changeset = proposed
        write_evidence(
            self.operations / "implement-proposal.json",
            context=self.evidence_context(),
            payload={"request": request, "changeset": proposed},
        )
        applied = apply_changeset(self.workspace, proposed, self.inputs.expected_allowed_paths)
        self.applied = applied
        snapshot = self.git.snapshot(result.repository)
        write_evidence(
            self.operations / "implement-applied.json",
            context=self.evidence_context(),
            payload={"applied": applied, "workspace_snapshot": _snapshot_evidence(snapshot)},
        )
        return applied

    def validate(self) -> bool:
        repository = self._require_workspace()
        receipts = []
        passed = True
        for check in (*self.inputs.item_validation_checks, *self.inputs.scope_required_checks):
            result = self.processes.run(
                CommandSpec(
                    argv=check.argv,
                    cwd=repository.root,
                    timeout_seconds=self.validation_timeout_seconds,
                )
            )
            receipts.append(result.receipt)
            if result.receipt.status is not ProcessStatus.SUCCEEDED:
                passed = False
        try:
            diff = self.git.diff_check(repository)
        except Exception as exc:
            raise ValidationExecutionError("built-in Git diff check failed") from exc
        self.validation_receipts = tuple(receipts)
        self.diff_check = diff
        self.validation_passed = passed and diff.ok
        write_evidence(
            self.operations / "validation.json",
            context=self.evidence_context(),
            payload={
                "commands": self.validation_receipts,
                "diff_check": diff,
                "passed": self.validation_passed,
            },
        )
        return self.validation_passed

    def review(self) -> tuple[bool, tuple[str, ...]]:
        repository = self._require_workspace()
        applied = self._require_applied()
        snapshot = self.git.snapshot(repository)
        findings: list[str] = []
        expected = {item.path for item in applied.files}
        actual = {
            path.as_posix()
            for group in (
                snapshot.staged_paths,
                snapshot.unstaged_paths,
                snapshot.untracked_paths,
                snapshot.conflicted_paths,
            )
            for path in group
        }
        if snapshot.branch != self.inputs.scope_branch:
            findings.append("workspace_branch_mismatch")
        if snapshot.head_sha != self.inputs.baseline_head:
            findings.append("workspace_head_changed_before_commit")
        if snapshot.staged_paths:
            findings.append("unexpected_staged_changes")
        if snapshot.conflicted_paths:
            findings.append("workspace_conflicts")
        if actual != expected:
            findings.append("changed_paths_mismatch")
        for item in applied.files:
            candidate = repository.root.joinpath(*item.path.split("/"))
            if not candidate.is_file() or candidate.is_symlink():
                findings.append(f"final_file_missing:{item.path}")
            elif _sha256(candidate.read_bytes()) != item.after_sha256:
                findings.append(f"final_hash_mismatch:{item.path}")
        if not self.validation_passed or self.diff_check is None or not self.diff_check.ok:
            findings.append("validation_not_passed")
        try:
            self.live_recheck("review_baseline_drift")
        except WriteBaselineDriftError:
            findings.append("review_baseline_drift")
        self.review_findings = tuple(dict.fromkeys(findings))
        self.review_passed = not self.review_findings
        write_evidence(
            self.operations / "review.json",
            context=self.evidence_context(),
            payload={"passed": self.review_passed, "findings": self.review_findings},
        )
        return self.review_passed, self.review_findings

    def commit(self) -> str:
        if not self.review_passed:
            raise CommitVerificationError("review has not authorized close")
        self.live_recheck("write_baseline_drift_before_commit")
        repository = self._require_workspace()
        applied = self._require_applied()
        before = self.git.snapshot(repository)
        if before.staged_paths or before.conflicted_paths:
            raise CommitVerificationError("workspace changed after review")
        expected = tuple(item.path for item in applied.files)
        actual = {
            path.as_posix()
            for group in (before.unstaged_paths, before.untracked_paths)
            for path in group
        }
        if actual != set(expected):
            raise CommitVerificationError("workspace paths changed after review")
        for item in applied.files:
            candidate = repository.root.joinpath(*item.path.split("/"))
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or _sha256(candidate.read_bytes()) != item.after_sha256
            ):
                raise CommitVerificationError("reviewed file content changed before commit")
        final_diff_check = self.git.diff_check(repository)
        if not final_diff_check.ok:
            raise CommitVerificationError("workspace diff check changed after review")
        stage = self.git.stage_paths(repository, expected)
        result = self.git.commit(
            repository,
            f"agentgraph({self.inputs.package.item_id}): {self.inputs.package.title}",
            expected_paths=expected,
            identity=self.commit_identity,
        )
        final = self.git.snapshot(repository)
        if (
            final.dirty
            or final.branch != self.inputs.scope_branch
            or final.head_sha != result.commit_sha
        ):
            raise CommitVerificationError("committed workspace verification failed")
        if (
            self.git.resolve_ref(self.target, f"refs/heads/{self.inputs.scope_branch}")
            != result.commit_sha
        ):
            raise CommitVerificationError("scope branch ref does not equal commit")
        self.live_recheck("write_baseline_drift_after_commit")
        self.commit_sha = result.commit_sha
        write_evidence(
            self.operations / "commit.json",
            context=self.evidence_context(),
            payload={
                "commit_sha": result.commit_sha,
                "stage_receipt": stage,
                "commit_receipt": result.receipt,
            },
        )
        return result.commit_sha

    def live_recheck(self, code: str) -> None:
        current = self.git.snapshot(self.target)
        pinned = self.shadow.inspection.git_snapshot
        if (
            current.head_sha != self.inputs.baseline_head
            or current.branch != self.inputs.base_branch
            or current.detached_head
            or current.dirty
            or current.conflicted_paths
            or pinned.head_sha != self.inputs.baseline_head
        ):
            raise WriteBaselineDriftError(code)
        snapshot = self.source.snapshot()
        try:
            verify_work_source_revision(self.target.root, snapshot.revision)
        except Exception as exc:
            raise WriteBaselineDriftError(code) from exc
        if snapshot.revision.fingerprint != self.inputs.source_revision:
            raise WriteBaselineDriftError(code)

    def change_request(self) -> ChangeRequest:
        package = self.inputs.package
        return ChangeRequest(
            self.inputs.project_id,
            package.item_id,
            package.scope_id,
            package.title,
            package.goal,
            package.acceptance_criteria,
            package.test_requirements,
            self.inputs.expected_allowed_paths,
            self.inputs.source_revision,
            self.inputs.baseline_head,
            (
                "external_runtime_worktree_only",
                "target_main_worktree_read_only",
                "one_item_zero_repairs",
                "no_source_closure",
                "no_push_or_pull_request",
            ),
        )

    def evidence_context(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "project_id": self.inputs.project_id,
            "item_id": self.inputs.package.item_id,
            "scope_id": self.inputs.package.scope_id,
            "pinned_head": self.inputs.baseline_head,
            "source_revision": self.inputs.source_revision,
            "changeset_digest": None if self.changeset is None else self.changeset.digest,
        }

    def _require_workspace(self) -> GitRepository:
        if self.workspace_repository is None:
            raise WorkspaceError("external workspace has not been created")
        return self.workspace_repository

    def _require_applied(self) -> AppliedChangeSet:
        if self.applied is None:
            raise WorkspaceError("changes have not been applied")
        return self.applied


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _snapshot_evidence(snapshot) -> dict[str, object]:
    return {
        "head_sha": snapshot.head_sha,
        "branch": snapshot.branch,
        "detached_head": snapshot.detached_head,
        "staged_paths": tuple(path.as_posix() for path in snapshot.staged_paths),
        "unstaged_paths": tuple(path.as_posix() for path in snapshot.unstaged_paths),
        "untracked_paths": tuple(path.as_posix() for path in snapshot.untracked_paths),
        "conflicted_paths": tuple(path.as_posix() for path in snapshot.conflicted_paths),
        "dirty": snapshot.dirty,
    }
