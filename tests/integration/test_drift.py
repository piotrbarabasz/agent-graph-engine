from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.infra import GitAdapter
from agentgraph.integration import ShadowOutcome, ShadowRequest
from agentgraph.work import InvalidWorkSourceError, WorkSourceValidation

from .conftest import make_runner


class DriftingWorkSource:
    def __init__(
        self,
        delegate,
        *,
        invalidate: bool = False,
        repository_mismatch: bool = False,
    ) -> None:
        self.delegate = delegate
        self.invalidate = invalidate
        self.repository_mismatch = repository_mismatch
        self.snapshot_calls = 0

    def validate(self):
        return self.delegate.validate()

    def snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_calls > 1:
            if self.invalidate:
                raise InvalidWorkSourceError(WorkSourceValidation())
            snapshot = self.delegate.snapshot()
            if self.repository_mismatch:
                first, *remaining = snapshot.revision.documents
                mismatched = replace(first, sha256="sha256:" + "0" * 64)
                revision = replace(snapshot.revision, documents=(mismatched, *remaining))
                return replace(snapshot, revision=revision)
            revision = replace(snapshot.revision, fingerprint="sha256:" + "f" * 64)
            return replace(snapshot, revision=revision)
        return self.delegate.snapshot()

    def get_scope(self, snapshot, scope_id):
        return self.delegate.get_scope(snapshot, scope_id)

    def get_item(self, snapshot, item_id):
        return self.delegate.get_item(snapshot, item_id)

    def next_ready_item(self, snapshot, scope_id):
        return self.delegate.next_ready_item(snapshot, scope_id)

    def next_ready_scope(self, snapshot, parent_scope_id):
        return self.delegate.next_ready_scope(snapshot, parent_scope_id)

    def build_package(self, snapshot, item_id):
        return self.delegate.build_package(snapshot, item_id)


class DriftingGitAdapter:
    def __init__(self, delegate: GitAdapter) -> None:
        self.delegate = delegate
        self.snapshot_calls = 0

    def discover_repository(self, path):
        return self.delegate.discover_repository(path)

    def snapshot(self, repository):
        self.snapshot_calls += 1
        snapshot = self.delegate.snapshot(repository)
        if self.snapshot_calls > 1:
            return replace(
                snapshot,
                untracked_paths=(Path("appeared.txt"),),
                dirty=True,
            )
        return snapshot


def test_work_source_revision_drift_never_returns_ready(target, tmp_path) -> None:
    source = DriftingWorkSource(SpecKitAdapter(SpecKitLayout(target)))

    report = make_runner(target, tmp_path / "runtime", work_source=source).run(
        ShadowRequest(scope_id="E001")
    )

    assert report.outcome is ShadowOutcome.DRIFTED
    assert report.graph_state is not None
    assert report.graph_state.graph.current_node == "EXPLORE"
    assert "source_drift" in {issue.code for issue in report.issues}


def test_invalidated_source_during_final_check_is_drift(target, tmp_path) -> None:
    source = DriftingWorkSource(SpecKitAdapter(SpecKitLayout(target)), invalidate=True)

    report = make_runner(target, tmp_path / "runtime", work_source=source).run(
        ShadowRequest(scope_id="E001")
    )

    assert report.outcome is ShadowOutcome.DRIFTED
    assert "shadow_inputs_invalidated" in {issue.code for issue in report.issues}


def test_repository_semantic_drift_never_returns_ready(target, tmp_path) -> None:
    adapter = DriftingGitAdapter(GitAdapter())

    report = make_runner(target, tmp_path / "runtime", git_adapter=adapter).run(
        ShadowRequest(scope_id="E001")
    )

    assert report.outcome is ShadowOutcome.DRIFTED
    assert "repository_drift" in {issue.code for issue in report.issues}


def test_final_source_documents_are_reverified_against_target(target, tmp_path) -> None:
    source = DriftingWorkSource(
        SpecKitAdapter(SpecKitLayout(target)),
        repository_mismatch=True,
    )

    report = make_runner(target, tmp_path / "runtime", work_source=source).run(
        ShadowRequest(scope_id="E001")
    )

    assert report.outcome is ShadowOutcome.DRIFTED
    assert "work_source_repository_mismatch" in {issue.code for issue in report.issues}
