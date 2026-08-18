from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.core import CheckpointOutcome, RunStatus
from agentgraph.infra import GitAdapter, GitCommitIdentity
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.runtime.codec import utc_now
from agentgraph.write import (
    PublishCheckpointView,
    RemotePullRequest,
    RemoteRepositoryIdentity,
    WriteSliceOutcome,
    WriteSliceRequest,
    WriteSliceRunner,
)
from tests.integration.conftest import git
from tests.integration.test_m012_multi_item import PerItemProvider, _multi_target
from tests.integration.test_m013_delivery_review import DeliveryReviewer


class IdentityGitAdapter(GitAdapter):
    def remote_push_url(self, repository, remote_name):
        super().remote_push_url(repository, remote_name)
        return "https://github.com/owner/repository.git"

    def push_exact_branch(self, repository, **kwargs):
        self.push_calls = getattr(self, "push_calls", 0) + 1
        return super().push_exact_branch(repository, **kwargs)


class FakeRemoteProvider:
    def __init__(self) -> None:
        self.repository = RemoteRepositoryIdentity("github.com", "123", "owner/repository")
        self.pull_requests = []
        self.create_calls = 0
        self.inspect_calls = 0
        self.find_calls = 0

    def inspect_repository(self, repository):
        self.inspect_calls += 1
        assert repository.full_name == self.repository.full_name
        return self.repository

    def find_open_pull_requests(self, repository, *, head_branch, base_branch):
        self.find_calls += 1
        return tuple(
            item
            for item in self.pull_requests
            if item.repository == repository
            and item.head_branch == head_branch
            and item.base_branch == base_branch
        )

    def create_draft_pull_request(self, request):
        self.create_calls += 1
        pull_request = RemotePullRequest(
            self.repository,
            "456",
            7,
            "https://github.com/owner/repository/pull/7",
            "open",
            True,
            request.head_branch,
            request.final_head,
            request.base_branch,
            request.title,
            request.body,
        )
        self.pull_requests.append(pull_request)
        return pull_request


def _runner(
    target: Path,
    runtime: Path,
    remote,
    adapter,
    *,
    fault=None,
    clock=None,
    checkpoint_ttl_seconds=86400,
) -> WriteSliceRunner:
    paths = RuntimePaths.resolve(runtime)
    return WriteSliceRunner(
        target,
        SpecKitAdapter(SpecKitLayout(target)),
        PerItemProvider(),
        delivery_review_agent_provider=DeliveryReviewer(),
        remote_provider=remote,
        git_adapter=adapter,
        project_registry=ProjectRegistry(paths, project_id_factory=lambda: "prj_m014"),
        commit_identity=GitCommitIdentity("M014 Test", "m014@example.test"),
        run_id_factory=lambda: "run_m014",
        max_work_items_per_run=3,
        checkpoint_nonce_factory=lambda: "m014-nonce",
        clock=clock or utc_now,
        checkpoint_ttl_seconds=checkpoint_ttl_seconds,
        fault=fault,
    )


def _target_with_remote(tmp_path):
    target = _multi_target(tmp_path)
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")
    git(target, "remote", "add", "origin", str(bare))
    adapter = IdentityGitAdapter(executable=shutil.which("git") or "git")
    return target, bare, adapter


def _single_target_with_remote(tmp_path):
    target = _multi_target(tmp_path, count=1)
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")
    git(target, "remote", "add", "origin", str(bare))
    adapter = IdentityGitAdapter(executable=shutil.which("git") or "git")
    return target, bare, adapter


def test_approved_exact_publication_pushes_final_head_and_creates_one_draft(tmp_path) -> None:
    target, bare, adapter = _target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    runner = _runner(target, tmp_path / "runtime", remote, adapter)

    pending = runner.run(WriteSliceRequest(scope_id="E001"))

    assert pending.outcome is WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED
    assert isinstance(pending.checkpoint, PublishCheckpointView)
    assert remote.pull_requests == []
    assert (
        adapter.remote_branch_sha(adapter.discover_repository(target), "origin", "work/e001")
        is None
    )

    runner.submit_checkpoint(
        pending.run_id or "",
        checkpoint_id=pending.checkpoint.checkpoint_id,
        nonce=pending.checkpoint.nonce,
        outcome=CheckpointOutcome.APPROVED,
        actor="m014-reviewer",
    )
    completed = runner.resume(pending.run_id or "")

    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert completed.graph_state is not None
    assert completed.graph_state.run.status is RunStatus.COMPLETED
    assert completed.publish is not None
    assert completed.publish.draft is True
    assert completed.publish.head_sha == completed.commit_sha
    assert remote.create_calls == 1
    assert len(remote.pull_requests) == 1
    assert adapter.push_calls == 1
    assert git(bare, "rev-parse", "refs/heads/work/e001").decode().strip() == (completed.commit_sha)

    repeated = runner.resume(pending.run_id or "")
    assert repeated.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert repeated.publish == completed.publish
    assert remote.create_calls == 1


def test_rejected_and_cancelled_publish_decisions_have_no_remote_effect(tmp_path) -> None:
    for outcome, expected_status in (
        (CheckpointOutcome.REJECTED, RunStatus.BLOCKED),
        (CheckpointOutcome.CANCELLED, RunStatus.CANCELLED),
    ):
        case = tmp_path / outcome.value
        case.mkdir()
        target, _bare, adapter = _single_target_with_remote(case)
        remote = FakeRemoteProvider()
        initial_remote = None if outcome is CheckpointOutcome.REJECTED else remote
        runner = _runner(target, case / "runtime", initial_remote, adapter)
        pending = runner.run(WriteSliceRequest(scope_id="E001"))
        if initial_remote is None:
            assert pending.outcome is WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
            runner = _runner(target, case / "runtime", remote, adapter)
            pending = runner.resume(pending.run_id or "")
        assert isinstance(pending.checkpoint, PublishCheckpointView)

        runner.submit_checkpoint(
            pending.run_id or "",
            checkpoint_id=pending.checkpoint.checkpoint_id,
            nonce=pending.checkpoint.nonce,
            outcome=outcome,
            actor="m014-reviewer",
        )
        final = runner.resume(pending.run_id or "")

        assert final.graph_state is not None
        assert final.graph_state.run.status is expected_status
        assert remote.pull_requests == []
        assert (
            adapter.remote_branch_sha(adapter.discover_repository(target), "origin", "work/e001")
            is None
        )


class OneShotFault:
    def __init__(self, stage: str, occurrence: int = 1) -> None:
        self.stage = stage
        self.occurrence = occurrence
        self.seen = 0
        self.triggered = False

    def __call__(self, stage: str) -> None:
        if stage == self.stage:
            self.seen += 1
        if stage == self.stage and self.seen == self.occurrence and not self.triggered:
            self.triggered = True
            raise RuntimeError("injected M014 interruption")


@pytest.mark.parametrize(
    "stage",
    (
        "after_publish_push",
        "after_publish_push_receipt",
        "after_publish_pr_create",
        "after_publish_pr_receipt",
        "after_publish_result",
    ),
)
def test_interrupted_create_pr_reconciles_without_duplicate(tmp_path, stage) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    fault = OneShotFault(stage)
    runner = _runner(target, tmp_path / "runtime", remote, adapter, fault=fault)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    assert isinstance(pending.checkpoint, PublishCheckpointView)
    runner.submit_checkpoint(
        pending.run_id or "",
        checkpoint_id=pending.checkpoint.checkpoint_id,
        nonce=pending.checkpoint.nonce,
        outcome=CheckpointOutcome.APPROVED,
        actor="m014-reviewer",
    )

    try:
        runner.resume(pending.run_id or "")
    except RuntimeError as exc:
        assert str(exc) == "injected M014 interruption"
    else:
        raise AssertionError("fault did not interrupt CREATE_PR")

    resumed = _runner(target, tmp_path / "runtime", remote, adapter).resume(pending.run_id or "")
    assert resumed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert remote.create_calls == 1
    assert len(remote.pull_requests) == 1
    assert adapter.push_calls == 1
