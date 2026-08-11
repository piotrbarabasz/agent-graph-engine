"""Precise errors raised by Graph Core contracts."""


class AgentGraphError(Exception):
    """Base error for deterministic graph-core failures."""


class ContractValidationError(AgentGraphError):
    """A public contract is internally inconsistent."""


class GraphDefinitionError(AgentGraphError):
    """A graph definition is malformed."""


class GraphTransitionError(AgentGraphError):
    """A deterministic transition cannot be selected."""


class NoValidTransitionError(GraphTransitionError):
    """No outgoing edge matches the typed state and result."""


class AmbiguousTransitionError(GraphTransitionError):
    """Multiple matching edges share the highest priority."""


class StatePatchError(AgentGraphError):
    """A state patch cannot be applied safely."""


class StaleStatePatchError(StatePatchError):
    """A patch was produced from an outdated state version."""


class UnauthorizedStatePatchError(StatePatchError):
    """A node attempted to modify a path it does not own."""


class InvalidStatePatchError(StatePatchError):
    """A patch path, operation, value, or resulting state is invalid."""
