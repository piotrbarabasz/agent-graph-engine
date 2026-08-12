from __future__ import annotations

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.core import GraphEngine, PolicySnapshot, canonical_v1_graph
from agentgraph.integration import (
    ShadowInputs,
    ShadowRequest,
    assess_preflight,
    inspect_project,
    prepare_selection,
)
from agentgraph.nodes import (
    DiscoverProjectNode,
    FinalizeNode,
    PreflightNode,
    SelectWorkNode,
    StartNode,
)
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from tests.integration.conftest import initialize_target, make_runner


def test_node_registry_contains_no_explore_or_write_capability(tmp_path) -> None:
    target = tmp_path / "target"
    initialize_target(target)
    runner = make_runner(target, tmp_path / "runtime")
    report = runner.run(ShadowRequest(scope_id="E001"))

    assert report.executed_nodes == ("START", "DISCOVER_PROJECT", "PREFLIGHT", "SELECT_WORK")
    assert {"EXPLORE", "BUILD_TASK_PACKAGE", "ASSESS_RISK", "IMPLEMENT", "REVIEW"}.isdisjoint(
        report.executed_nodes
    )


def test_each_concrete_node_declares_zero_external_effects(tmp_path) -> None:
    target = tmp_path / "target"
    initialize_target(target)
    source = SpecKitAdapter(SpecKitLayout(target))
    registry = ProjectRegistry(
        RuntimePaths.resolve(tmp_path / "runtime"),
        project_id_factory=lambda: "prj_nodes",
    )
    runner = make_runner(target, tmp_path / "unused")
    inspection = inspect_project(
        target,
        git_adapter=runner.git_adapter,
        project_registry=registry,
        work_source=source,
    )
    selection, package = prepare_selection(source, inspection.work_snapshot, ShadowRequest("E001"))
    preflight = assess_preflight(inspection, selection)
    inputs = ShadowInputs(inspection, preflight, selection, package, "sha256:test")
    policy = PolicySnapshot()
    engine = GraphEngine(canonical_v1_graph(), policy)
    state = engine.initial_state("shadow_nodes")
    nodes = (
        StartNode(),
        DiscoverProjectNode(inputs),
        PreflightNode(inputs),
        SelectWorkNode(inputs),
        FinalizeNode(),
    )

    for node in nodes:
        from dataclasses import replace

        from agentgraph.core.state import GraphProgress

        node_state = replace(state, graph=GraphProgress(current_node=node.node_id))
        context = GraphEngine(canonical_v1_graph(), policy).build_node_context(node_state)
        assert node.run(node_state, context).external_effects == ()
