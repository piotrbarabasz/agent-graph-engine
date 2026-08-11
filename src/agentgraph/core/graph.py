"""Validated static graph definition."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .edges import Edge
from .errors import GraphDefinitionError
from .node import NodeDefinition


@dataclass(frozen=True, slots=True)
class GraphDefinition:
    """Immutable nodes and edges validated without runtime dependencies."""

    nodes: tuple[NodeDefinition, ...]
    edges: tuple[Edge, ...]
    _nodes_by_id: Mapping[str, NodeDefinition] = field(init=False, repr=False)
    _outgoing: Mapping[str, tuple[Edge, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.nodes, tuple) or not isinstance(self.edges, tuple):
            raise GraphDefinitionError("nodes and edges must be immutable tuples")
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise GraphDefinitionError("node IDs must be unique")
        if "START" not in node_ids:
            raise GraphDefinitionError("graph must define START")
        if "END" not in node_ids:
            raise GraphDefinitionError("graph must define END")

        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise GraphDefinitionError("edge IDs must be unique")
        outgoing: dict[str, list[Edge]] = {node_id: [] for node_id in node_ids}
        for edge in self.edges:
            self._validate_edge(edge, node_ids)
            outgoing[edge.from_node].append(edge)
        if outgoing["END"]:
            raise GraphDefinitionError("END cannot have outgoing edges")
        if any(edge.to_node == "END" and not edge.terminal for edge in self.edges):
            raise GraphDefinitionError("every edge into END must be terminal")
        if any(edge.terminal and edge.to_node != "END" for edge in self.edges):
            raise GraphDefinitionError("terminal edges must target END")

        object.__setattr__(
            self,
            "_nodes_by_id",
            MappingProxyType(dict(zip(node_ids, self.nodes, strict=True))),
        )
        object.__setattr__(
            self,
            "_outgoing",
            MappingProxyType({key: tuple(value) for key, value in outgoing.items()}),
        )

    @staticmethod
    def _validate_edge(edge: Edge, node_ids: list[str]) -> None:
        if not isinstance(edge, Edge):
            raise GraphDefinitionError("edges must contain Edge values")
        if not edge.id or not edge.from_node or not edge.to_node:
            raise GraphDefinitionError("edge id/from/to are required")
        if edge.from_node not in node_ids:
            raise GraphDefinitionError(f"unknown from_node {edge.from_node}")
        if edge.to_node not in node_ids:
            raise GraphDefinitionError(f"unknown to_node {edge.to_node}")
        if type(edge.priority) is not int or edge.priority < 0:
            raise GraphDefinitionError("edge priority must be a non-negative integer")
        if not callable(getattr(edge.condition, "matches", None)):
            raise GraphDefinitionError("edge condition must implement matches")
        if not all(callable(getattr(guard, "evaluate", None)) for guard in edge.guards):
            raise GraphDefinitionError("edge guards must implement evaluate")
        if edge.checkpoint and edge.resume_node is None:
            raise GraphDefinitionError("checkpoint edge requires resume_node")
        if edge.resume_node is not None and edge.resume_node not in node_ids:
            raise GraphDefinitionError(f"unknown checkpoint resume node {edge.resume_node}")

    def node(self, node_id: str) -> NodeDefinition:
        """Return one declared node or raise a graph-definition error."""

        try:
            return self._nodes_by_id[node_id]
        except KeyError as exc:
            raise GraphDefinitionError(f"unknown node {node_id}") from exc

    def outgoing(self, node_id: str) -> tuple[Edge, ...]:
        """Return the immutable outgoing edge set for a declared node."""

        try:
            return self._outgoing[node_id]
        except KeyError as exc:
            raise GraphDefinitionError(f"unknown node {node_id}") from exc
