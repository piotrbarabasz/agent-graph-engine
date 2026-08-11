from __future__ import annotations

from dataclasses import replace

import pytest

from agentgraph.core import GraphEngine, GraphState, PolicySnapshot
from agentgraph.core.state import GraphProgress
from agentgraph.core.v1_graph import canonical_v1_graph


@pytest.fixture
def policy() -> PolicySnapshot:
    return PolicySnapshot(max_repair_cycles=2, max_work_items_per_run=3)


@pytest.fixture
def engine(policy: PolicySnapshot) -> GraphEngine:
    return GraphEngine(canonical_v1_graph(), policy)


def state_at(engine: GraphEngine, node_id: str) -> GraphState:
    state = engine.initial_state("test-run")
    return replace(state, graph=GraphProgress(current_node=node_id))
