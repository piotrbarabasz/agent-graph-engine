"""Typed state patches and central ownership enforcement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from types import MappingProxyType
from typing import Any

from .enums import (
    FailureCategory,
    OperationType,
    RepairClassification,
    ReviewVerdict,
    RiskLevel,
    ValidationVerdict,
)
from .errors import (
    ContractValidationError,
    InvalidStatePatchError,
    StaleStatePatchError,
    UnauthorizedStatePatchError,
)
from .state import (
    DeliveryScope,
    GraphState,
    RepairRecord,
    WorkHierarchyItem,
    WorkItem,
)


@dataclass(frozen=True, slots=True)
class PatchOperation:
    """One typed operation against a canonical dotted state path."""

    operation: OperationType
    path: str
    value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, OperationType):
            raise ContractValidationError("operation must be an OperationType")
        if not self.path or self.path.startswith(".") or self.path.endswith("."):
            raise ContractValidationError("patch path must be a dotted field path")
        if self.operation is OperationType.CLEAR and self.value is not None:
            raise ContractValidationError("clear does not accept a value")

    @classmethod
    def set(cls, path: str, value: Any) -> PatchOperation:
        """Create a set operation."""

        return cls(OperationType.SET, path, value)

    @classmethod
    def append_unique(cls, path: str, value: Any) -> PatchOperation:
        """Create an append-unique operation."""

        return cls(OperationType.APPEND_UNIQUE, path, value)

    @classmethod
    def increment(cls, path: str, value: int = 1) -> PatchOperation:
        """Create an integer increment operation."""

        return cls(OperationType.INCREMENT, path, value)

    @classmethod
    def merge_by_id(cls, path: str, value: Any) -> PatchOperation:
        """Create a tuple merge-by-id operation."""

        return cls(OperationType.MERGE_BY_ID, path, value)

    @classmethod
    def clear(cls, path: str) -> PatchOperation:
        """Create a clear operation."""

        return cls(OperationType.CLEAR, path)


@dataclass(frozen=True, slots=True)
class StatePatch:
    """Ordered operations produced from one exact state version."""

    base_state_version: int
    operations: tuple[PatchOperation, ...] = ()

    def __post_init__(self) -> None:
        if type(self.base_state_version) is not int or self.base_state_version < 0:
            raise ContractValidationError("base_state_version cannot be negative")
        if not isinstance(self.operations, tuple):
            raise ContractValidationError("operations must be an immutable tuple")
        if not all(isinstance(item, PatchOperation) for item in self.operations):
            raise ContractValidationError("operations must contain PatchOperation values")


@dataclass(frozen=True, slots=True)
class PathRule:
    """Legal operations and value types for one state leaf."""

    operations: frozenset[OperationType]
    value_types: tuple[type, ...]
    nullable: bool = False
    item_types: tuple[type, ...] = ()


SET = frozenset({OperationType.SET, OperationType.CLEAR})
SET_ONLY = frozenset({OperationType.SET})
TUPLE_OPS = frozenset(
    {OperationType.SET, OperationType.APPEND_UNIQUE, OperationType.MERGE_BY_ID, OperationType.CLEAR}
)


PATH_RULES: Mapping[str, PathRule] = MappingProxyType(
    {
        "repository.identifier": PathRule(SET, (str,), True),
        "repository.metadata": PathRule(SET, (Mapping,)),
        "project.name": PathRule(SET, (str,), True),
        "project.metadata": PathRule(SET, (Mapping,)),
        "work.source": PathRule(SET, (str,), True),
        "work.hierarchy": PathRule(TUPLE_OPS, (tuple,), item_types=(WorkHierarchyItem,)),
        "work.item": PathRule(SET, (WorkItem,), True),
        "work.completed_items": PathRule(TUPLE_OPS, (tuple,), item_types=(WorkItem,)),
        "work.dependencies": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "work.delivery_scope": PathRule(SET, (DeliveryScope,), True),
        "work.available_items": PathRule(TUPLE_OPS, (tuple,), item_types=(WorkItem,)),
        "task_package.ready": PathRule(SET_ONLY, (bool,)),
        "task_package.metadata": PathRule(SET, (Mapping,)),
        "requirements.items": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "acceptance_criteria.items": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "architecture_invariants.items": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "baseline.revision": PathRule(SET, (str,), True),
        "baseline.metadata": PathRule(SET, (Mapping,)),
        "scope.included": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "scope.excluded": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "risk.level": PathRule(SET, (RiskLevel,), True),
        "changes.agent_reported_files": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "changes.identifiers": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "changes.count": PathRule(
            frozenset({OperationType.SET, OperationType.INCREMENT}),
            (int,),
        ),
        "validation.verdict": PathRule(SET_ONLY, (ValidationVerdict,)),
        "validation.checks": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "review.verdict": PathRule(SET_ONLY, (ReviewVerdict,)),
        "review.safe_to_close": PathRule(SET_ONLY, (bool,)),
        "review.findings": PathRule(TUPLE_OPS, (tuple,), item_types=(str,)),
        "repair.classification": PathRule(SET, (RepairClassification,), True),
        "repair.history": PathRule(TUPLE_OPS, (tuple,), item_types=(RepairRecord,)),
        "failure.category": PathRule(SET, (FailureCategory,), True),
        "failure.code": PathRule(SET, (str,), True),
        "cancellation.requested": PathRule(SET_ONLY, (bool,)),
        "cancellation.reason": PathRule(SET, (str,), True),
    }
)


class PatchOwnershipPolicy:
    """Single reusable authority for all engine/node-owned state paths."""

    engine_owned_paths = frozenset(
        {
            "schema_version",
            "state_version",
            "run.status",
            "repair.count",
            "repair.max_cycles",
            "risk.programmer_route",
            "scope.locked",
        }
    )
    engine_owned_prefixes = ("graph", "checkpoints", "commits", "push", "pull_request")

    def authorize(
        self,
        *,
        state: GraphState,
        node_id: str,
        allowed_paths: tuple[str, ...],
        path: str,
    ) -> None:
        """Raise when a node does not own the requested canonical path."""

        if path in self.engine_owned_paths or any(
            path == prefix or path.startswith(f"{prefix}.") for prefix in self.engine_owned_prefixes
        ):
            raise UnauthorizedStatePatchError(f"{node_id} cannot patch engine-owned path {path}")
        if state.scope.locked and path.startswith("scope."):
            raise UnauthorizedStatePatchError(f"{node_id} cannot patch locked scope path {path}")
        if not any(_path_matches(path, pattern) for pattern in allowed_paths):
            raise UnauthorizedStatePatchError(f"{node_id} is not allowed to patch {path}")


def _path_matches(path: str, pattern: str) -> bool:
    return (
        pattern == "*"
        or path == pattern
        or (pattern.endswith(".*") and path.startswith(pattern[:-1]))
    )


class StatePatchApplier:
    """Validate and atomically apply a node patch to an immutable snapshot."""

    def __init__(self, ownership: PatchOwnershipPolicy | None = None) -> None:
        self.ownership = ownership or PatchOwnershipPolicy()

    def apply(
        self,
        state: GraphState,
        patch: StatePatch,
        *,
        node_id: str,
        allowed_paths: tuple[str, ...],
        increment_version: bool = True,
    ) -> GraphState:
        """Apply all operations atomically after authorization and type checks."""

        if patch.base_state_version != state.state_version:
            raise StaleStatePatchError(
                f"patch version {patch.base_state_version} != state version {state.state_version}"
            )
        candidate = state
        try:
            for operation in patch.operations:
                self.ownership.authorize(
                    state=state,
                    node_id=node_id,
                    allowed_paths=allowed_paths,
                    path=operation.path,
                )
                candidate = self._apply_operation(candidate, operation)
            if increment_version:
                candidate = replace(candidate, state_version=state.state_version + 1)
            return candidate
        except (StaleStatePatchError, UnauthorizedStatePatchError, InvalidStatePatchError):
            raise
        except (AttributeError, TypeError, ValueError, ContractValidationError) as exc:
            raise InvalidStatePatchError(str(exc)) from exc

    def _apply_operation(self, state: GraphState, operation: PatchOperation) -> GraphState:
        rule = PATH_RULES.get(operation.path)
        if rule is None:
            raise InvalidStatePatchError(f"unknown or non-patchable path {operation.path}")
        if operation.operation not in rule.operations:
            raise InvalidStatePatchError(f"{operation.operation} is not legal for {operation.path}")
        current = _get_path(state, operation.path)
        value = _operation_value(current, operation, rule)
        return _replace_path(state, operation.path, value)


def _operation_value(current: Any, operation: PatchOperation, rule: PathRule) -> Any:
    if operation.operation is OperationType.CLEAR:
        if rule.nullable:
            return None
        if tuple in rule.value_types:
            return ()
        if Mapping in rule.value_types:
            return MappingProxyType({})
        raise InvalidStatePatchError(f"{operation.path} cannot be cleared")

    value = operation.value
    if operation.operation is OperationType.SET:
        if value is None and rule.nullable:
            return None
        if not isinstance(value, rule.value_types):
            expected = ", ".join(item.__name__ for item in rule.value_types)
            raise InvalidStatePatchError(f"{operation.path} requires {expected}")
        if isinstance(value, tuple) and rule.item_types:
            _validate_items(value, rule.item_types, operation.path)
        if isinstance(value, Mapping):
            return MappingProxyType(dict(value))
        return value

    if operation.operation is OperationType.APPEND_UNIQUE:
        if not isinstance(current, tuple):
            raise InvalidStatePatchError("append_unique requires a tuple target")
        _validate_items((value,), rule.item_types, operation.path)
        return current if value in current else (*current, value)

    if operation.operation is OperationType.MERGE_BY_ID:
        if not isinstance(current, tuple):
            raise InvalidStatePatchError("merge_by_id requires a tuple target")
        _validate_items((value,), rule.item_types, operation.path)
        identifier = _item_id(value)
        merged = tuple(value if _item_id(item) == identifier else item for item in current)
        already_present = any(_item_id(item) == identifier for item in current)
        return merged if already_present else (*current, value)

    if operation.operation is OperationType.INCREMENT:
        if type(current) is not int or type(value) is not int:  # bool is deliberately excluded
            raise InvalidStatePatchError("increment requires integer target and value")
        return current + value
    raise InvalidStatePatchError(f"unsupported operation {operation.operation}")


def _validate_items(items: tuple[Any, ...], item_types: tuple[type, ...], path: str) -> None:
    if not item_types or not all(isinstance(item, item_types) for item in items):
        raise InvalidStatePatchError(f"invalid item type for {path}")


def _item_id(item: Any) -> str:
    if is_dataclass(item) and hasattr(item, "id"):
        return str(item.id)
    if isinstance(item, Mapping) and "id" in item:
        return str(item["id"])
    raise InvalidStatePatchError("merge_by_id item must have an id")


def _get_path(root: Any, path: str) -> Any:
    current = root
    for part in path.split("."):
        current = getattr(current, part)
    return current


def _replace_path(root: Any, path: str, value: Any) -> Any:
    parts = path.split(".")
    if not is_dataclass(root):
        raise InvalidStatePatchError(f"cannot descend into {parts[0]}")
    known_fields = {item.name for item in fields(root)}
    if parts[0] not in known_fields:
        raise InvalidStatePatchError(f"unknown path {path}")
    if len(parts) == 1:
        return replace(root, **{parts[0]: value})
    child = getattr(root, parts[0])
    replaced_child = _replace_path(child, ".".join(parts[1:]), value)
    return replace(root, **{parts[0]: replaced_child})
