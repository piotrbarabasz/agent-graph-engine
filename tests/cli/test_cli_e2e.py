from __future__ import annotations

from pathlib import Path

from agentgraph.cli import ProviderOverrides, build_application
from agentgraph.core import CheckpointOutcome
from agentgraph.infra import CommandReceipt, GitPushResult, ProcessStatus, ProcessTermination
from agentgraph.write import WriteSliceOutcome
from tests.integration.conftest import git
from tests.integration.test_m012_multi_item import PerItemProvider
from tests.integration.test_m013_delivery_review import DeliveryReviewer
from tests.integration.test_m014_publish import (
    FakeRemoteProvider,
    IdentityGitAdapter,
    _target_with_remote,
)

from .test_status_checkpoint import FilesystemDeclaredProvider


class SimulatedPublishGitAdapter(IdentityGitAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.remote_heads: dict[str, str] = {}
        self.push_calls = 0

    def remote_branch_sha_at_endpoint(self, repository, endpoint, branch):
        assert endpoint.url == "https://github.com/owner/repository.git"
        return self.remote_heads.get(branch)

    def push_exact_branch_to_endpoint(self, repository, *, endpoint, commit_sha, remote_branch):
        assert endpoint.url == "https://github.com/owner/repository.git"
        self.push_calls += 1
        self.remote_heads[remote_branch] = commit_sha
        receipt = CommandReceipt(
            "cmd_m015_simulated_push",
            ("git", "push"),
            str(repository.root),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            0,
            ProcessStatus.SUCCEEDED,
            0,
            0,
            0,
            False,
            False,
            ProcessTermination.NONE,
        )
        return GitPushResult(commit_sha, endpoint.url, remote_branch, receipt)


def test_full_v1_application_flow_publishes_once_after_separate_approval_and_resume(
    tmp_path: Path,
    tmp_path_factory,
    config_text: str,
    monkeypatch,
) -> None:
    target, _bare, _adapter = _target_with_remote(tmp_path)
    adapter = SimulatedPublishGitAdapter()
    (target / ".agentgraph.yml").write_text(
        config_text.replace(
            "  semantic: true\n  delivery: true",
            "  semantic: false\n  delivery: true",
        ),
        encoding="utf-8",
    )
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "agentgraph config")
    runtime = tmp_path_factory.mktemp("v1")
    remote = FakeRemoteProvider()
    provider = PerItemProvider()
    monkeypatch.setenv("GITHUB_TOKEN", "M015_TOKEN_SENTINEL")
    app = build_application(
        target,
        runtime_home=runtime,
        provider_overrides=ProviderOverrides(
            git_adapter=adapter,
            change_provider=provider,
            general_agent_provider=FilesystemDeclaredProvider(),
            delivery_review_provider=DeliveryReviewer(),
            remote_provider=remote,
        ),
    )

    pending = app.run("E001", None)

    assert pending.outcome is WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED
    assert pending.run_id is not None
    assert pending.checkpoint is not None
    assert pending.completed_item_ids == ("T001", "T002")
    before_decision_calls = (remote.inspect_calls, remote.find_calls, remote.create_calls)
    selected, checkpoint = app.show_checkpoint(pending.run_id)
    assert selected == pending.run_id
    assert checkpoint.checkpoint_type == "publish"
    assert checkpoint.draft is True

    app.submit_checkpoint(
        pending.run_id,
        outcome=CheckpointOutcome.APPROVED,
        actor="Piotr",
    )

    assert (remote.inspect_calls, remote.find_calls, remote.create_calls) == before_decision_calls
    assert adapter.remote_heads == {}

    completed = app.resume(pending.run_id)

    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert remote.create_calls == 1
    assert len(remote.pull_requests) == 1
    assert adapter.remote_heads["work/e001"] == completed.commit_sha
    assert adapter.push_calls == 1
    status_calls = (remote.inspect_calls, remote.find_calls, remote.create_calls)
    first = app.status(pending.run_id)
    second = app.status(pending.run_id)
    assert first == second
    assert first.profile_bound
    assert first.profile_match is True
    assert first.publish is not None
    assert first.publish.pr_url == "https://github.com/owner/repository/pull/7"
    assert (remote.inspect_calls, remote.find_calls, remote.create_calls) == status_calls
    assert all(
        b"M015_TOKEN_SENTINEL" not in path.read_bytes()
        for path in runtime.rglob("*")
        if path.is_file()
    )

    repeated = app.resume(pending.run_id)
    assert repeated.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert remote.create_calls == 1
