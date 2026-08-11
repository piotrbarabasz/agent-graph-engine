"""Pure guard contracts for transition authorization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .policy import PolicySnapshot
from .result import NodeResult
from .state import GraphState


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Typed result of a side-effect-free guard evaluation."""

    passed: bool
    code: str
    reason: str


class Guard(Protocol):
    """A pure transition guard."""

    def evaluate(
        self, state: GraphState, result: NodeResult, policy: PolicySnapshot
    ) -> GuardResult:
        """Return a decision without changing state or causing side effects."""


@dataclass(frozen=True, slots=True)
class AllowGuard:
    """Convenient pure guard that always permits a transition."""

    code: str = "allowed"

    def evaluate(
        self, state: GraphState, result: NodeResult, policy: PolicySnapshot
    ) -> GuardResult:
        del state, result, policy
        return GuardResult(True, self.code, "guard allowed transition")


@dataclass(frozen=True, slots=True)
class DenyGuard:
    """Convenient pure guard that always rejects a transition."""

    code: str = "denied"

    def evaluate(
        self, state: GraphState, result: NodeResult, policy: PolicySnapshot
    ) -> GuardResult:
        del state, result, policy
        return GuardResult(False, self.code, "guard rejected transition")
