"""Small deterministic synchronous graph engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from .edges import Transition
from .enums import ProgrammerRoute, RiskLevel, RunStatus
from .errors import (
    AmbiguousTransitionError,
    ContractValidationError,
    GraphTransitionError,
    NoValidTransitionError,
)
from .graph import GraphDefinition
from .node import Node, NodeContext
from .patches import StatePatchApplier
from .policy import PolicySnapshot
from .result import NodeResult
from .state import (
    ChangesState,
    FailureState,
    GraphProgress,
    GraphState,
    RepairState,
    ReviewState,
    RiskState,
    ScopeState,
    TaskPackageState,
    TextCollectionState,
    ValidationState,
)


class GraphEngine:
    """Validate results, apply patches, and select exactly one transition."""

    def __init__(
        self,
        graph: GraphDefinition,
        policy: PolicySnapshot,
        nodes: Mapping[str, Node] | None = None,
        *,
        patch_applier: StatePatchApplier | None = None,
    ) -> None:
        self.graph = graph
        self.policy = policy
        self.nodes = dict(nodes or {})
        self.patch_applier = patch_applier or StatePatchApplier()

    def initial_state(self, run_id: str) -> GraphState:
        """Create policy-aligned state at START."""

        return GraphState.initial(run_id, max_repair_cycles=self.policy.max_repair_cycles)

    def evaluate_transition(self, state: GraphState, result: NodeResult) -> Transition:
        """Select the sole guarded edge at the highest matching priority."""

        self._validate_result_identity(state, result)
        matches: list[tuple[object, tuple[object, ...]]] = []
        for edge in self.graph.outgoing(state.graph.current_node):
            if not edge.condition.matches(state, result, self.policy):
                continue
            guard_results = tuple(
                guard.evaluate(state, result, self.policy) for guard in edge.guards
            )
            if all(item.passed for item in guard_results):
                matches.append((edge, guard_results))

        if not matches:
            raise NoValidTransitionError(
                f"no transition from {state.graph.current_node} for {result.status}"
            )
        highest_priority = max(item[0].priority for item in matches)
        winners = [item for item in matches if item[0].priority == highest_priority]
        if len(winners) != 1:
            ids = ", ".join(item[0].id for item in winners)
            raise AmbiguousTransitionError(
                f"ambiguous transitions from {state.graph.current_node}: {ids}"
            )
        edge, guard_results = winners[0]
        return Transition(
            edge_id=edge.id,
            from_node=edge.from_node,
            to_node=edge.to_node,
            priority=edge.priority,
            guard_results=guard_results,
            checkpoint=edge.checkpoint,
            terminal=edge.terminal,
            final_status=edge.final_status,
            resume_node=edge.resume_node,
        )

    def apply_result(self, state: GraphState, result: NodeResult) -> tuple[GraphState, Transition]:
        """Atomically apply one valid node result and its engine-owned transition effects."""

        self._validate_state_policy(state)
        self._validate_result_identity(state, result)
        definition = self.graph.node(state.graph.current_node)
        candidate = state
        if result.state_patch is not None:
            candidate = self.patch_applier.apply(
                state,
                result.state_patch,
                node_id=result.node_id,
                allowed_paths=definition.allowed_patch_paths,
                increment_version=False,
            )
        transition = self.evaluate_transition(candidate, result)
        candidate = self._execute_transition(candidate, transition)
        candidate = replace(candidate, state_version=state.state_version + 1)
        self._validate_state_policy(candidate)
        return candidate, transition

    def step(
        self,
        state: GraphState,
        *,
        deadline: datetime | None = None,
    ) -> tuple[GraphState, NodeResult, Transition]:
        """Run the injected node at the current cursor and apply its result."""

        node_id = state.graph.current_node
        if node_id == "END":
            raise GraphTransitionError("END is terminal and cannot be stepped")
        try:
            node = self.nodes[node_id]
        except KeyError as exc:
            raise ContractValidationError(f"no runtime node injected for {node_id}") from exc
        attempt_id = f"{state.run.run_id}:{node_id}:{state.graph.transition_seq + 1}"
        definition = self.graph.node(node_id)
        context = NodeContext(
            run_id=state.run.run_id,
            node_attempt_id=attempt_id,
            idempotency_key=attempt_id,
            deadline=deadline,
            policy_snapshot=self.policy,
            allowed_state_patch_paths=definition.allowed_patch_paths,
        )
        result = node.run(state, context)
        if result.attempt_id != attempt_id:
            raise ContractValidationError("node result attempt_id does not match context")
        next_state, transition = self.apply_result(state, result)
        return next_state, result, transition

    def run(self, state: GraphState, *, max_steps: int = 1_000) -> GraphState:
        """Execute injected in-memory nodes until END or a safety step bound."""

        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        current = state
        for _ in range(max_steps):
            if current.graph.current_node == "END":
                return current
            current, _, _ = self.step(current)
        raise GraphTransitionError(f"run exceeded max_steps={max_steps}")

    def _execute_transition(self, state: GraphState, transition: Transition) -> GraphState:
        if transition.from_node == "MORE_WORK" and transition.to_node == "SELECT_WORK":
            state = self._reset_item_state(state)
        if transition.to_node == "DELIVERY_REVIEW":
            state = replace(state, review=ReviewState())

        repair = state.repair
        risk = state.risk
        scope = state.scope
        run = state.run

        if transition.to_node in {"PROGRAMMER_REPAIR", "DEBUGGER"}:
            if repair.count >= self.policy.max_repair_cycles:
                raise GraphTransitionError("repair transition exceeds policy limit")
            repair = replace(repair, count=repair.count + 1)

        if transition.to_node == "IMPLEMENT":
            if risk.level in {RiskLevel.LOW, RiskLevel.MEDIUM}:
                route = ProgrammerRoute.FAST
            elif risk.level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
                route = ProgrammerRoute.HIGH
            else:
                raise GraphTransitionError("IMPLEMENT requires an assessed risk level")
            risk = replace(risk, programmer_route=route)
            scope = replace(scope, locked=True)

        pending_resume = state.graph.pending_resume_node
        if transition.checkpoint:
            pending_resume = transition.resume_node
        elif state.graph.current_node == "HUMAN_CHECKPOINT":
            pending_resume = None

        status = transition.final_status or run.status
        if transition.terminal and status is RunStatus.RUNNING:
            status = RunStatus.COMPLETED
        graph = GraphProgress(
            current_node=transition.to_node,
            previous_node=state.graph.current_node,
            transition_seq=state.graph.transition_seq + 1,
            pending_resume_node=pending_resume,
        )
        return replace(
            state,
            graph=graph,
            repair=repair,
            risk=risk,
            scope=scope,
            run=replace(run, status=status),
        )

    def _reset_item_state(self, state: GraphState) -> GraphState:
        """Start a fresh item scope while preserving run- and delivery-level state."""

        return replace(
            state,
            task_package=TaskPackageState(),
            requirements=TextCollectionState(),
            acceptance_criteria=TextCollectionState(),
            scope=ScopeState(),
            risk=RiskState(),
            changes=ChangesState(),
            validation=ValidationState(),
            review=ReviewState(),
            failure=FailureState(),
            repair=RepairState(max_cycles=self.policy.max_repair_cycles),
        )

    def _validate_result_identity(self, state: GraphState, result: NodeResult) -> None:
        if result.node_id != state.graph.current_node:
            raise ContractValidationError(
                f"result for {result.node_id} cannot be applied at {state.graph.current_node}"
            )

    def _validate_state_policy(self, state: GraphState) -> None:
        if state.repair.max_cycles != self.policy.max_repair_cycles:
            raise ContractValidationError("state repair limit differs from immutable policy")
        if state.repair.count > self.policy.max_repair_cycles:
            raise ContractValidationError("state repair count exceeds immutable policy")
