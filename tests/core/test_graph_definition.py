import pytest

from agentgraph.core import Edge, GraphDefinition, GraphDefinitionError, NodeDefinition, NodeType
from agentgraph.core.edges import AlwaysCondition


def node(node_id: str) -> NodeDefinition:
    return NodeDefinition(node_id, NodeType.DETERMINISTIC)


def edge(edge_id: str, source: str, target: str, **kwargs: object) -> Edge:
    return Edge(edge_id, source, target, 1, AlwaysCondition(), **kwargs)  # type: ignore[arg-type]


def test_valid_minimal_graph() -> None:
    graph = GraphDefinition(
        (node("START"), node("END")),
        (edge("finish", "START", "END", terminal=True),),
    )
    assert graph.outgoing("START")[0].id == "finish"


@pytest.mark.parametrize(
    ("nodes", "edges", "match"),
    [
        ((node("START"), node("START"), node("END")), (), "unique"),
        (
            (node("START"), node("END")),
            (edge("x", "START", "END", terminal=True), edge("x", "START", "END", terminal=True)),
            "unique",
        ),
        ((node("END"),), (), "START"),
        ((node("START"),), (), "END"),
        ((node("START"), node("END")), (edge("x", "MISSING", "END", terminal=True),), "from_node"),
        ((node("START"), node("END")), (edge("x", "START", "MISSING"),), "to_node"),
    ],
)
def test_invalid_graph_references_are_rejected(
    nodes: tuple[NodeDefinition, ...], edges: tuple[Edge, ...], match: str
) -> None:
    with pytest.raises(GraphDefinitionError, match=match):
        GraphDefinition(nodes, edges)


def test_malformed_edge_is_rejected() -> None:
    malformed = Edge("x", "START", "END", -1, AlwaysCondition(), terminal=True)
    with pytest.raises(GraphDefinitionError, match="priority"):
        GraphDefinition((node("START"), node("END")), (malformed,))


def test_terminal_semantics_are_validated() -> None:
    with pytest.raises(GraphDefinitionError, match="terminal"):
        GraphDefinition(
            (node("START"), node("END")),
            (edge("finish", "START", "END"),),
        )


def test_end_cannot_have_outgoing_edges() -> None:
    with pytest.raises(GraphDefinitionError, match="END cannot"):
        GraphDefinition(
            (node("START"), node("END")),
            (
                edge("finish", "START", "END", terminal=True),
                edge("bad", "END", "START"),
            ),
        )
