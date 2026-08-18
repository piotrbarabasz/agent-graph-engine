from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentgraph.core import CheckpointOutcome, RunStatus
from agentgraph.runtime.codec import (
    canonical_json_bytes,
    decode_value,
    parse_json_bytes,
    sha256_digest,
)
from agentgraph.write import (
    CheckpointError,
    GitHubRemoteProvider,
    HttpResponse,
    PublishCheckpointRequestRecord,
    PublishConflictError,
    PublishEvidenceError,
    PublishPlan,
    PullRequestReceipt,
    RemoteAuthenticationError,
    RemoteContractError,
    RemotePullRequest,
    RemoteServiceError,
    WriteSliceOutcome,
    WriteSliceRequest,
)
from agentgraph.write.evidence import read_evidence
from agentgraph.write.publish import PublishCheckpointController
from tests.integration.conftest import git
from tests.integration.test_m014_publish import (
    FakeRemoteProvider,
    IdentityGitAdapter,
    OneShotFault,
    _runner,
    _single_target_with_remote,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class UnfilteredRemoteProvider(FakeRemoteProvider):
    def find_open_pull_requests(self, repository, *, head_branch, base_branch):
        del repository, head_branch, base_branch
        self.find_calls += 1
        return tuple(self.pull_requests)


class InspectFailureProvider(FakeRemoteProvider):
    def __init__(self, error) -> None:
        super().__init__()
        self.error = error

    def inspect_repository(self, repository):
        self.inspect_calls += 1
        if self.error is not None:
            raise self.error
        assert repository.full_name == self.repository.full_name
        return self.repository


class CreateContractFailureProvider(FakeRemoteProvider):
    def create_draft_pull_request(self, request):
        del request
        self.create_calls += 1
        raise RemoteContractError("malformed GitHub response")


class ToggleFindFailureProvider(FakeRemoteProvider):
    unavailable = False

    def find_open_pull_requests(self, repository, *, head_branch, base_branch):
        if self.unavailable:
            raise RemoteServiceError("service unavailable")
        return super().find_open_pull_requests(
            repository, head_branch=head_branch, base_branch=base_branch
        )


class MultiplePushIdentityAdapter(IdentityGitAdapter):
    def remote_push_urls(self, repository, remote_name):
        super().remote_push_urls(repository, remote_name)
        return (
            "https://github.com/owner/repository.git",
            "https://github.com/owner/other.git",
        )


class GitHubTransport:
    def __init__(self) -> None:
        self.final_head = None

    def request(self, method, url, *, headers, body, max_response_bytes):
        del headers, max_response_bytes
        repository = {"id": 123, "full_name": "owner/repository"}
        if method == "GET" and url.endswith("/repos/owner/repository"):
            return HttpResponse(200, canonical_json_bytes(repository))
        if method == "GET" and "/pulls?" in url:
            return HttpResponse(200, b"[]")
        assert method == "POST" and url.endswith("/repos/owner/repository/pulls")
        assert self.final_head is not None
        request = parse_json_bytes(body)
        payload = {
            "id": 456,
            "number": 7,
            "html_url": "https://github.com/owner/repository/pull/7",
            "state": "open",
            "draft": True,
            "head": {
                "ref": request["head"],
                "sha": self.final_head,
                "repo": repository,
            },
            "base": {"ref": request["base"], "repo": repository},
            "title": request["title"],
            "body": request["body"],
        }
        return HttpResponse(201, canonical_json_bytes(payload))


def _run_path(report) -> Path:
    assert report.runtime_path is not None
    return Path(report.runtime_path)


def _plan(report) -> PublishPlan:
    document = read_evidence(_run_path(report) / "publish" / "plan.json")
    return decode_value(document["payload"], PublishPlan)


def _pull_request(plan: PublishPlan, **changes) -> RemotePullRequest:
    values = {
        "repository": FakeRemoteProvider().repository,
        "pr_id": "456",
        "number": 7,
        "url": "https://github.com/owner/repository/pull/7",
        "state": "open",
        "draft": True,
        "head_branch": plan.remote_head_branch,
        "head_sha": plan.final_head,
        "base_branch": plan.base_branch,
        "title": plan.pr_title,
        "body": plan.pr_body,
    }
    values.update(changes)
    return RemotePullRequest(**values)


def _approve(runner, pending) -> None:
    runner.submit_checkpoint(
        pending.run_id or "",
        checkpoint_id=pending.checkpoint.checkpoint_id,
        nonce=pending.checkpoint.nonce,
        outcome=CheckpointOutcome.APPROVED,
        actor="m014-reviewer",
    )


def _rewrite_self_digested(path: Path, field: str, value: object, digest_field: str) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    document[field] = value
    document[digest_field] = sha256_digest(
        {key: item for key, item in document.items() if key != digest_field}
    )
    path.write_bytes(canonical_json_bytes(document))


def _rewrite_publish_payload(path: Path, **changes: object) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    payload = document["payload"]
    payload.update(changes)
    payload["digest"] = sha256_digest(
        {key: item for key, item in payload.items() if key != "digest"}
    )
    document["content_digest"] = sha256_digest(
        {key: item for key, item in document.items() if key != "content_digest"}
    )
    path.write_bytes(canonical_json_bytes(document))


def test_multiple_push_urls_are_rejected_before_checkpoint_or_remote_mutation(tmp_path) -> None:
    target, bare, _adapter = _single_target_with_remote(tmp_path)
    adapter = MultiplePushIdentityAdapter()
    remote = FakeRemoteProvider()

    final = _runner(target, tmp_path / "runtime", remote, adapter).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert final.outcome is WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED
    assert final.issues[0].code == "multiple_push_targets_not_supported"
    assert final.checkpoint is None
    assert final.graph_state.run.status is RunStatus.RUNNING
    assert remote.inspect_calls == 0
    assert remote.create_calls == 0
    assert git(bare, "for-each-ref", "--format=%(refname)") == b""


def test_remote_failure_after_plan_before_approval_retries_same_checkpoint(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = InspectFailureProvider(None)
    runtime = tmp_path / "runtime"
    runner = _runner(target, runtime, remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    remote.error = RemoteServiceError("service unavailable")

    blocked = runner.resume(pending.run_id or "")

    assert blocked.outcome is WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED
    assert blocked.graph_state.run.status is RunStatus.RUNNING
    assert blocked.graph_state.graph.current_node == "HUMAN_CHECKPOINT"
    assert blocked.graph_state.state_version == pending.graph_state.state_version
    remote.error = None
    retried = runner.resume(pending.run_id or "")
    assert retried.outcome is WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED
    assert retried.checkpoint.checkpoint_id == pending.checkpoint.checkpoint_id


def test_approved_human_decision_is_consumed_without_network(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = InspectFailureProvider(None)
    runtime = tmp_path / "runtime"
    runner = _runner(target, runtime, remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    calls = remote.inspect_calls
    remote.error = RemoteServiceError("service unavailable")

    final = _runner(target, runtime, remote, adapter).resume(pending.run_id or "")

    assert remote.inspect_calls == calls + 1
    assert final.graph_state.graph.current_node == "END"
    assert final.issues[0].code == "remote_service_unavailable"
    assert remote.create_calls == 0
    assert getattr(adapter, "push_calls", 0) == 0


def test_finalized_local_chain_detects_recomputed_semantic_tamper_and_needs_no_provider(
    tmp_path,
) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    runtime = tmp_path / "runtime"
    runner = _runner(target, runtime, remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    completed = runner.resume(pending.run_id or "")
    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    publish = _run_path(completed) / "publish"
    cases = (
        (
            publish / "result.json",
            {
                "remote_repository_full_name": "other/repository",
                "pr_url": "https://github.com/other/repository/pull/7",
            },
        ),
        (
            publish / "result.json",
            {"checkpoint_request_digest": "sha256:" + "0" * 64},
        ),
        (
            publish / "pull-request.json",
            {
                "remote_repository_full_name": "other/repository",
                "pr_url": "https://github.com/other/repository/pull/7",
            },
        ),
        (
            publish / "push.json",
            {"performed_push": False, "command_receipt": None},
        ),
    )
    for path, changes in cases:
        original = path.read_bytes()
        _rewrite_publish_payload(path, **changes)
        tampered = _runner(target, runtime, None, adapter).resume(pending.run_id or "")
        assert tampered.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
        path.write_bytes(original)

    local = _runner(target, runtime, None, adapter).resume(pending.run_id or "")
    assert local.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert local.publish == completed.publish

    result_path = publish / "result.json"
    result_bytes = result_path.read_bytes()
    result_path.unlink()
    missing = _runner(target, runtime, None, adapter).resume(pending.run_id or "")
    assert missing.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert missing.issues[0].code == "publish_result_missing"
    result_path.write_bytes(result_bytes)


def test_historical_m013_boundary_stays_inert_until_provider_is_supplied(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    runtime = tmp_path / "runtime"
    historical = _runner(target, runtime, None, adapter).run(WriteSliceRequest(scope_id="E001"))

    assert historical.outcome is WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
    assert not (_run_path(historical) / "publish").exists()
    assert not (_run_path(historical) / "checkpoints").exists()

    resumed = _runner(target, runtime, FakeRemoteProvider(), adapter).resume(
        historical.run_id or ""
    )
    assert resumed.outcome is WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED
    assert (_run_path(resumed) / "publish" / "plan.json").is_file()
    assert (
        _run_path(resumed) / "checkpoints" / resumed.checkpoint.checkpoint_id / "request.json"
    ).is_file()


def test_expired_publish_checkpoint_has_no_remote_effect(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    clock = Clock()
    runner = _runner(
        target,
        tmp_path / "runtime",
        remote,
        adapter,
        clock=clock,
        checkpoint_ttl_seconds=60,
    )
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    clock.advance(61)

    with pytest.raises(CheckpointError, match="checkpoint_expired"):
        _approve(runner, pending)
    final = runner.resume(pending.run_id or "")

    assert final.outcome is WriteSliceOutcome.BLOCKED
    assert final.issues[0].code == "checkpoint_expired"
    assert remote.create_calls == 0
    assert getattr(adapter, "push_calls", 0) == 0


def test_publish_request_and_decision_semantic_tamper_fail_closed(tmp_path) -> None:
    for evidence_name in ("request.json", "decision.json"):
        case = tmp_path / evidence_name.removesuffix(".json")
        case.mkdir()
        target, _bare, adapter = _single_target_with_remote(case)
        remote = FakeRemoteProvider()
        runner = _runner(target, case / "runtime", remote, adapter)
        pending = runner.run(WriteSliceRequest(scope_id="E001"))
        if evidence_name == "decision.json":
            _approve(runner, pending)
        path = _run_path(pending) / "checkpoints" / pending.checkpoint.checkpoint_id / evidence_name
        if evidence_name == "request.json":
            _rewrite_self_digested(path, "base_branch", "tampered", "request_digest")
        else:
            _rewrite_self_digested(path, "request_digest", "sha256:" + "0" * 64, "decision_digest")

        final = _runner(target, case / "runtime", remote, adapter).resume(pending.run_id or "")

        assert final.outcome in {WriteSliceOutcome.BLOCKED, WriteSliceOutcome.RECOVERY_REQUIRED}
        assert remote.create_calls == 0
        assert getattr(adapter, "push_calls", 0) == 0


def test_approved_publish_plan_tamper_blocks_before_remote_effect(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    runner = _runner(target, tmp_path / "runtime", remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    path = _run_path(pending) / "publish" / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["pr_title"] = "tampered title"
    unsigned = {key: value for key, value in document.items() if key != "content_digest"}
    document["content_digest"] = sha256_digest(unsigned)
    path.write_bytes(canonical_json_bytes(document))

    final = _runner(target, tmp_path / "runtime", remote, adapter).resume(pending.run_id or "")

    assert final.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert remote.create_calls == 0
    assert getattr(adapter, "push_calls", 0) == 0


def test_conflicting_remote_branch_is_rejected_before_publish_checkpoint(tmp_path) -> None:
    target, bare, adapter = _single_target_with_remote(tmp_path)
    baseline = git(target, "rev-parse", "HEAD").decode().strip()
    git(target, "push", "origin", f"{baseline}:refs/heads/work/e001")
    remote = FakeRemoteProvider()

    final = _runner(target, tmp_path / "runtime", remote, adapter).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert final.outcome is WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED
    assert final.issues[0].code == "remote_branch_conflict"
    assert final.checkpoint is None
    assert remote.create_calls == 0
    assert git(bare, "rev-parse", "refs/heads/work/e001").decode().strip() == baseline


def test_same_remote_head_is_an_allowed_noop_push(tmp_path) -> None:
    target, bare, adapter = _single_target_with_remote(tmp_path)
    runtime = tmp_path / "runtime"
    historical = _runner(target, runtime, None, adapter).run(WriteSliceRequest(scope_id="E001"))
    assert historical.commit_sha is not None
    git(
        target,
        "push",
        "origin",
        f"{historical.commit_sha}:refs/heads/work/e001",
    )
    remote = FakeRemoteProvider()
    runner = _runner(target, runtime, remote, adapter)
    pending = runner.resume(historical.run_id or "")
    _approve(runner, pending)

    completed = runner.resume(pending.run_id or "")

    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert getattr(adapter, "push_calls", 0) == 0
    assert git(bare, "rev-parse", "refs/heads/work/e001").decode().strip() == completed.commit_sha


@pytest.mark.parametrize(
    "variant", ("unrelated", "wrong_sha", "wrong_base", "not_draft", "multiple")
)
def test_non_exact_or_ambiguous_pull_requests_are_never_adopted(tmp_path, variant) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = UnfilteredRemoteProvider()
    runner = _runner(target, tmp_path / "runtime", remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    plan = _plan(pending)
    changes = {
        "unrelated": {"body": "unrelated body"},
        "wrong_sha": {"head_sha": "f" * 40},
        "wrong_base": {"base_branch": "release"},
        "not_draft": {"draft": False},
        "multiple": {},
    }[variant]
    remote.pull_requests.append(_pull_request(plan, **changes))
    if variant == "multiple":
        remote.pull_requests.append(
            _pull_request(
                plan,
                pr_id="789",
                number=8,
                url="https://github.com/owner/repository/pull/8",
            )
        )
    with pytest.raises(PublishConflictError, match="pull_request_conflict"):
        _approve(runner, pending)

    assert remote.create_calls == 0
    assert getattr(adapter, "push_calls", 0) == 0


def test_exact_existing_marker_pull_request_is_adopted_without_duplicate(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = UnfilteredRemoteProvider()
    runner = _runner(target, tmp_path / "runtime", remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    plan = _plan(pending)
    remote.pull_requests.append(_pull_request(plan))
    _approve(runner, pending)

    completed = runner.resume(pending.run_id or "")
    receipt_document = read_evidence(_run_path(completed) / "publish" / "pull-request.json")
    receipt = decode_value(receipt_document["payload"], PullRequestReceipt)

    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert remote.create_calls == 0
    assert receipt.adopted_existing is True


@pytest.mark.parametrize(
    "error",
    (RemoteAuthenticationError("authentication failed"), RemoteServiceError("service unavailable")),
)
def test_remote_operational_failure_is_nonterminal_and_retryable(tmp_path, error) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = InspectFailureProvider(error)

    runtime = tmp_path / "runtime"
    final = _runner(target, runtime, remote, adapter).run(WriteSliceRequest(scope_id="E001"))

    assert final.outcome is WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED
    assert final.graph_state.run.status is RunStatus.RUNNING
    assert final.graph_state.graph.current_node == "HUMAN_CHECKPOINT"
    assert final.graph_state.graph.pending_resume_node == "CREATE_PR"
    assert final.publish is None
    assert remote.create_calls == 0
    assert getattr(adapter, "push_calls", 0) == 0

    remote.error = None
    retried = _runner(target, runtime, remote, adapter).resume(final.run_id or "")

    assert retried.outcome is WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED


def test_malformed_create_response_is_terminal_contract_failure(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = CreateContractFailureProvider()
    runner = _runner(target, tmp_path / "runtime", remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)

    final = runner.resume(pending.run_id or "")

    assert final.outcome is WriteSliceOutcome.FAILED
    assert final.graph_state.run.status is RunStatus.FAILED
    assert final.issues[0].code == "remote_response_contract_invalid"
    assert final.publish is None


def test_github_token_never_enters_any_durable_run_evidence(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    sentinel = "m014-never-persist-this-token"
    transport = GitHubTransport()
    remote = GitHubRemoteProvider(transport, token_provider=lambda: sentinel)
    runner = _runner(target, tmp_path / "runtime", remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    transport.final_head = pending.commit_sha
    _approve(runner, pending)

    completed = runner.resume(pending.run_id or "")

    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    encoded = sentinel.encode()
    assert all(
        encoded not in path.read_bytes()
        for path in _run_path(completed).rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize(
    ("stage", "occurrence"),
    (
        ("after_node_started", 2),
        ("after_node_result_recorded", 2),
        ("after_transition_committed", 2),
    ),
)
def test_runtime_crash_boundaries_recover_without_duplicate_side_effect(
    tmp_path, stage, occurrence
) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    runtime = tmp_path / "runtime"
    runner = _runner(target, runtime, remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    crashing = _runner(
        target,
        runtime,
        remote,
        adapter,
        fault=OneShotFault(stage, occurrence),
    )
    with pytest.raises(RuntimeError, match="injected M014 interruption"):
        crashing.resume(pending.run_id or "")

    completed = _runner(target, runtime, remote, adapter).resume(pending.run_id or "")

    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert remote.create_calls == 1
    assert getattr(adapter, "push_calls", 0) == 1


def test_interrupted_create_pr_with_remote_branch_conflict_requires_recovery(tmp_path) -> None:
    target, bare, adapter = _single_target_with_remote(tmp_path)
    baseline = git(target, "rev-parse", "HEAD").decode().strip()
    remote = FakeRemoteProvider()
    runtime = tmp_path / "runtime"
    fault = OneShotFault("after_publish_push")
    runner = _runner(target, runtime, remote, adapter, fault=fault)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    with pytest.raises(RuntimeError, match="injected M014 interruption"):
        runner.resume(pending.run_id or "")
    git(bare, "update-ref", "refs/heads/work/e001", baseline)

    recovered = _runner(target, runtime, remote, adapter).resume(pending.run_id or "")

    assert recovered.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert recovered.issues[0].code == "remote_branch_conflict"
    assert remote.create_calls == 0


def test_interrupted_create_pr_never_claims_safe_rerun_when_service_is_unavailable(
    tmp_path,
) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = ToggleFindFailureProvider()
    runtime = tmp_path / "runtime"
    runner = _runner(
        target,
        runtime,
        remote,
        adapter,
        fault=OneShotFault("after_publish_push"),
    )
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    with pytest.raises(RuntimeError, match="injected M014 interruption"):
        runner.resume(pending.run_id or "")
    remote.unavailable = True

    recovered = _runner(target, runtime, remote, adapter).resume(pending.run_id or "")

    assert recovered.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert recovered.issues[0].code == "publish_reconciliation_unavailable"
    assert remote.create_calls == 0


@pytest.mark.parametrize(
    ("stage", "evidence_name", "changes"),
    (
        (
            "after_publish_push_receipt",
            "push.json",
            {"remote_repository_full_name": "other/repository"},
        ),
        (
            "after_publish_pr_receipt",
            "pull-request.json",
            {
                "remote_repository_full_name": "other/repository",
                "pr_url": "https://github.com/other/repository/pull/7",
            },
        ),
        (
            "after_publish_result",
            "result.json",
            {"checkpoint_decision_digest": "sha256:" + "0" * 64},
        ),
    ),
)
def test_partial_self_digested_publication_corruption_requires_recovery(
    tmp_path, stage, evidence_name, changes
) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    runtime = tmp_path / "runtime"
    runner = _runner(
        target,
        runtime,
        remote,
        adapter,
        fault=OneShotFault(stage),
    )
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    with pytest.raises(RuntimeError, match="injected M014 interruption"):
        runner.resume(pending.run_id or "")
    path = _run_path(pending) / "publish" / evidence_name
    _rewrite_publish_payload(path, **changes)
    calls = (remote.create_calls, getattr(adapter, "push_calls", 0))

    recovered = _runner(target, runtime, remote, adapter).resume(pending.run_id or "")

    assert recovered.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert (remote.create_calls, getattr(adapter, "push_calls", 0)) == calls


def test_finalized_publish_is_historical_after_remote_pr_lifecycle_change(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    runtime = tmp_path / "runtime"
    runner = _runner(target, runtime, remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    _approve(runner, pending)
    completed = runner.resume(pending.run_id or "")
    calls = (remote.inspect_calls, remote.find_calls, remote.create_calls)
    remote.pull_requests.clear()

    repeated = _runner(target, runtime, remote, adapter).resume(pending.run_id or "")

    assert repeated.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    assert repeated.publish == completed.publish
    assert (remote.inspect_calls, remote.find_calls, remote.create_calls) == calls


def test_publish_plan_binding_covers_every_approved_authority_field(tmp_path) -> None:
    target, _bare, adapter = _single_target_with_remote(tmp_path)
    remote = FakeRemoteProvider()
    runner = _runner(target, tmp_path / "runtime", remote, adapter)
    pending = runner.run(WriteSliceRequest(scope_id="E001"))
    plan = _plan(pending)
    request_path = (
        _run_path(pending) / "checkpoints" / pending.checkpoint.checkpoint_id / "request.json"
    )
    request = decode_value(
        json.loads(request_path.read_text(encoding="utf-8")),
        PublishCheckpointRequestRecord,
    )
    values = {field.name: getattr(plan, field.name) for field in fields(plan)}
    mutations = (
        ("remote_repository_id", "other"),
        ("base_branch", "release"),
        ("remote_head_branch", "work/other"),
        ("final_head", "f" * 40),
        ("pr_title", "other title"),
        ("pr_body_digest", sha256_digest("other body")),
    )
    for field, value in mutations:
        changed = SimpleNamespace(**{**values, field: value})
        with pytest.raises(PublishEvidenceError, match="publish_checkpoint_binding_mismatch"):
            PublishCheckpointController._validate_static_plan_binding(request, changed)
