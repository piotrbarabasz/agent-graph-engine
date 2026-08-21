from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentgraph.cli import ProviderOverrides, build_application
from agentgraph.cli.output import from_status
from agentgraph.core import CheckpointOutcome
from agentgraph.write import WriteSliceOutcome
from tests.integration.conftest import git
from tests.integration.test_m012_multi_item import PerItemProvider
from tests.integration.test_m013_delivery_review import DeliveryReviewer
from tests.integration.test_m014_publish import FakeRemoteProvider, _target_with_remote
from tests.integration.test_m014_publish_failures import (
    _assert_complete_publish_chain_valid,
    _rewrite_complete_publish_chain,
)

from .test_cli_e2e import SimulatedPublishGitAdapter
from .test_status_checkpoint import FilesystemDeclaredProvider


@pytest.fixture(scope="module")
def published_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("m015-status-publish")
    target, _bare, _adapter = _target_with_remote(root)
    config = (
        Path("examples/agentgraph.yml")
        .read_text(encoding="utf-8")
        .replace(
            "  semantic: true\n  delivery: true",
            "  semantic: false\n  delivery: true",
        )
    )
    (target / ".agentgraph.yml").write_text(config, encoding="utf-8")
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "agentgraph config")
    runtime = Path(tempfile.mkdtemp(prefix="age-m015-status-"))
    adapter = SimulatedPublishGitAdapter()
    remote = FakeRemoteProvider()
    provider = PerItemProvider()
    reviewer = DeliveryReviewer()
    app = build_application(
        target,
        runtime_home=runtime,
        provider_overrides=ProviderOverrides(
            git_adapter=adapter,
            change_provider=provider,
            general_agent_provider=FilesystemDeclaredProvider(),
            delivery_review_provider=reviewer,
            remote_provider=remote,
        ),
    )
    pending = app.run("E001", None)
    assert pending.outcome is WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED
    assert pending.run_id is not None and pending.checkpoint is not None
    checkpoint_id = pending.checkpoint.checkpoint_id
    app.submit_checkpoint(
        pending.run_id,
        outcome=CheckpointOutcome.APPROVED,
        actor="M015 status verifier",
    )
    completed = app.resume(pending.run_id)
    assert completed.outcome is WriteSliceOutcome.DRAFT_PR_CREATED
    try:
        yield SimpleNamespace(
            app=app,
            run_id=pending.run_id,
            run_path=Path(completed.runtime_path or ""),
            checkpoint_id=checkpoint_id,
            provider=provider,
            reviewer=reviewer,
            remote=remote,
            adapter=adapter,
        )
    finally:
        shutil.rmtree(runtime)


def _tree(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "link", str(path.readlink()).encode()))
        elif path.is_dir():
            entries.append((relative, "directory", None))
        else:
            entries.append((relative, "file", path.read_bytes()))
    return tuple(entries)


def _counts(run) -> tuple[int, int, int, int, int, int]:
    return (
        len(run.provider.item_ids),
        len(run.reviewer.calls),
        run.remote.inspect_calls,
        run.remote.find_calls,
        run.remote.create_calls,
        run.adapter.push_calls,
    )


def _assert_recovery_read_only(run) -> None:
    before_tree = _tree(run.run_path)
    before_counts = _counts(run)

    first = from_status("status", run.app.status(run.run_id))
    second = from_status("status", run.app.status(run.run_id))

    assert first == second
    assert first.ok is False
    assert first.outcome == "RECOVERY_REQUIRED"
    assert any(issue.code == "publish_evidence_mismatch" for issue in first.issues)
    assert first.publish is None
    assert _tree(run.run_path) == before_tree
    assert _counts(run) == before_counts


def _mutate_then_restore(run, mutation) -> None:
    original = _tree(run.run_path)
    files = {
        relative: content
        for relative, kind, content in original
        if kind == "file" and content is not None
    }
    try:
        mutation()
        _assert_recovery_read_only(run)
    finally:
        for relative, content in files.items():
            path = run.run_path / relative
            if path.is_symlink():
                path.unlink()
            elif path.exists() and path.read_bytes() == content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def test_finalized_happy_status_is_fully_verified_and_read_only(published_run) -> None:
    before = _tree(published_run.run_path)
    counts = _counts(published_run)

    first = from_status("status", published_run.app.status(published_run.run_id))
    second = from_status("status", published_run.app.status(published_run.run_id))

    assert first == second
    assert first.ok is True
    assert first.outcome == "STATUS"
    assert first.publish is not None
    assert first.publish.pr_url == "https://github.com/owner/repository/pull/7"
    assert _tree(published_run.run_path) == before
    assert _counts(published_run) == counts


@pytest.mark.parametrize(
    "relative",
    (
        "publish/plan.json",
        "publish/result.json",
        "publish/push.json",
        "publish/pull-request.json",
    ),
)
def test_missing_terminal_publish_artifact_requires_recovery(published_run, relative: str) -> None:
    _mutate_then_restore(published_run, lambda: (published_run.run_path / relative).unlink())


@pytest.mark.parametrize("name", ("request.json", "decision.json"))
def test_missing_publish_checkpoint_authority_requires_recovery(published_run, name: str) -> None:
    path = published_run.run_path / "checkpoints" / published_run.checkpoint_id / name
    _mutate_then_restore(published_run, path.unlink)


def test_self_consistent_publish_chain_tamper_cannot_replace_delivery_authority(
    published_run,
) -> None:
    replacement = "sha256:" + "a" * 64

    def mutate() -> None:
        _rewrite_complete_publish_chain(
            published_run.run_path,
            published_run.checkpoint_id,
            delivery_manifest_digest=replacement,
        )
        _assert_complete_publish_chain_valid(published_run.run_path, published_run.checkpoint_id)

    _mutate_then_restore(published_run, mutate)


def test_publish_evidence_symlink_requires_recovery_where_supported(
    published_run, tmp_path: Path
) -> None:
    result = published_run.run_path / "publish" / "result.json"
    outside = tmp_path / "result.json"
    outside.write_bytes(result.read_bytes())

    def mutate() -> None:
        result.unlink()
        try:
            result.symlink_to(outside)
        except OSError:
            result.write_bytes(outside.read_bytes())
            pytest.skip("symlink creation is unavailable")

    _mutate_then_restore(published_run, mutate)
