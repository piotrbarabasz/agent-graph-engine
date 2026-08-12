from __future__ import annotations

from dataclasses import replace

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.integration import ShadowOutcome, ShadowRequest

from .conftest import make_runner


class MisbehavingPackageSource:
    def __init__(self, delegate, mutation) -> None:
        self.delegate = delegate
        self.mutation = mutation

    def validate(self):
        return self.delegate.validate()

    def snapshot(self):
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
        return self.mutation(self.delegate.build_package(snapshot, item_id))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda package: replace(package, item_id="T999"),
        lambda package: replace(package, scope_id="E999"),
        lambda package: replace(
            package,
            source_revision=replace(
                package.source_revision,
                fingerprint="sha256:" + "f" * 64,
            ),
        ),
    ],
    ids=("wrong-item", "wrong-scope", "stale-revision"),
)
def test_misbehaving_work_package_is_rejected_before_graph(target, tmp_path, mutation) -> None:
    source = MisbehavingPackageSource(SpecKitAdapter(SpecKitLayout(target)), mutation)

    report = make_runner(target, tmp_path / "runtime", work_source=source).run(
        ShadowRequest(scope_id="E001")
    )

    assert report.outcome is ShadowOutcome.INVALID_SOURCE
    assert report.graph_state is None
    assert report.issues[0].code == "work_package_mismatch"
