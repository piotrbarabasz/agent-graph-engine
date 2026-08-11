import pytest

from agentgraph.core import (
    AmbiguousTransitionError,
    Edge,
    GraphDefinition,
    GraphEngine,
    NodeDefinition,
    NodeResult,
    NodeStatus,
    NodeType,
    NoValidTransitionError,
    PolicySnapshot,
)
from agentgraph.core.edges import AlwaysCondition, ResultStatusCondition
from agentgraph.core.guards import DenyGuard


def make_engine(*edges: Edge) -> GraphEngine:
    graph = GraphDefinition(
        (
            NodeDefinition("START", NodeType.DETERMINISTIC),
            NodeDefinition("A", NodeType.DETERMINISTIC),
            NodeDefinition("END", NodeType.DETERMINISTIC),
        ),
        (*edges, Edge("finish", "A", "END", 1, AlwaysCondition(), terminal=True)),
    )
    return GraphEngine(graph, PolicySnapshot())


def result() -> NodeResult:
    return NodeResult("START", "attempt", NodeStatus.SUCCEEDED)


def test_exactly_one_matching_edge_is_selected() -> None:
    engine = make_engine(Edge("go", "START", "A", 10, AlwaysCondition()))
    assert engine.evaluate_transition(engine.initial_state("r"), result()).edge_id == "go"


def test_zero_matching_edges_raise_explicit_error() -> None:
    condition = ResultStatusCondition(frozenset({NodeStatus.FAILED}))
    engine = make_engine(Edge("no", "START", "A", 10, condition))
    with pytest.raises(NoValidTransitionError):
        engine.evaluate_transition(engine.initial_state("r"), result())


def test_same_priority_matching_edges_are_ambiguous() -> None:
    engine = make_engine(
        Edge("one", "START", "A", 10, AlwaysCondition()),
        Edge("two", "START", "END", 10, AlwaysCondition(), terminal=True),
    )
    with pytest.raises(AmbiguousTransitionError, match="one, two"):
        engine.evaluate_transition(engine.initial_state("r"), result())


def test_highest_priority_wins_deterministically() -> None:
    engine = make_engine(
        Edge("low", "START", "END", 1, AlwaysCondition(), terminal=True),
        Edge("high", "START", "A", 50, AlwaysCondition()),
    )
    assert engine.evaluate_transition(engine.initial_state("r"), result()).edge_id == "high"


def test_guard_rejection_removes_edge_from_candidates() -> None:
    engine = make_engine(Edge("denied", "START", "A", 10, AlwaysCondition(), (DenyGuard(),)))
    with pytest.raises(NoValidTransitionError):
        engine.evaluate_transition(engine.initial_state("r"), result())
