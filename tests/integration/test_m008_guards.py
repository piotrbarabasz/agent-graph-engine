from __future__ import annotations

from dataclasses import replace

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.write import WriteSliceOutcome, WriteSliceRequest
from tests.integration.conftest import git
from tests.integration.test_m006_vertical_slice import _target
from tests.integration.test_m008_analysis import (
    CapturingChangeProvider,
    RecordingAgentProvider,
    runner,
)


@pytest.mark.parametrize("mutation", ("tracked", "untracked", "staged", "head"))
def test_read_only_agent_target_mutation_fails_closed_without_cleanup(tmp_path, mutation) -> None:
    target = _target(tmp_path)

    def mutate(operation, context):
        if operation != "explore":
            return
        if mutation == "untracked":
            (context.repository_root / "agent.tmp").write_text("untracked\n", encoding="utf-8")
            return
        tracked = context.repository_root / "tracked.txt"
        tracked.write_text(f"{mutation}\n", encoding="utf-8")
        if mutation in {"staged", "head"}:
            git(context.repository_root, "add", "--", "tracked.txt")
        if mutation == "head":
            git(context.repository_root, "commit", "--quiet", "-m", "forbidden agent commit")

    agent = RecordingAgentProvider(mutate=mutate)
    change = CapturingChangeProvider()
    report = runner(target, tmp_path / "runtime", agent, change).run(
        WriteSliceRequest(scope_id="E001")
    )

    assert report.outcome is WriteSliceOutcome.FAILED
    assert report.issues[0].code == "agent_provider_mutated_repository"
    assert len(agent.requests) == 1
    assert change.requests == []
    assert git(target, "status", "--porcelain") or mutation == "head"


class DriftingWorkSource:
    def __init__(self, delegate):
        self.delegate = delegate
        self.drift = False

    def snapshot(self):
        snapshot = self.delegate.snapshot()
        if not self.drift:
            return snapshot
        revision = replace(snapshot.revision, fingerprint=f"sha256:{'f' * 64}")
        return replace(snapshot, revision=revision)

    def validate(self):
        return self.delegate.validate()

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


def test_source_drift_after_explore_blocks_before_build_provider(tmp_path) -> None:
    target = _target(tmp_path)
    source = DriftingWorkSource(SpecKitAdapter(SpecKitLayout(target)))

    class DriftAfterExplore:
        transitions = 0

        def __call__(self, stage):
            if stage == "after_transition_committed":
                self.transitions += 1
                if self.transitions == 5:
                    source.drift = True

    agent = RecordingAgentProvider()
    write_runner = runner(target, tmp_path / "runtime", agent, fault=DriftAfterExplore())
    write_runner.work_source = source
    report = write_runner.run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "agent_analysis_baseline_drift"
    assert [request.operation_id for request, _ in agent.requests] == ["explore"]


def test_target_head_drift_after_explore_blocks_before_build_provider(tmp_path) -> None:
    target = _target(tmp_path)

    class MoveHeadAfterExplore:
        transitions = 0

        def __call__(self, stage):
            if stage == "after_transition_committed":
                self.transitions += 1
                if self.transitions == 5:
                    (target / "tracked.txt").write_text("external change\n", encoding="utf-8")
                    git(target, "add", "--", "tracked.txt")
                    git(target, "commit", "--quiet", "-m", "external head drift")

    agent = RecordingAgentProvider()
    report = runner(
        target,
        tmp_path / "runtime",
        agent,
        fault=MoveHeadAfterExplore(),
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.BLOCKED
    assert report.issues[0].code == "agent_analysis_baseline_drift"
    assert [request.operation_id for request, _ in agent.requests] == ["explore"]
