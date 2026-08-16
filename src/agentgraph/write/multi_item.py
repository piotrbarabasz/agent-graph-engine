"""Frozen-plan, sequential per-item execution for one durable scope run."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agentgraph.core import (
    DeliveryScope,
    GraphState,
    NodeContext,
    NodeResult,
    NodeStatus,
    PatchOperation,
    ResultReason,
    StatePatch,
    WorkHierarchyItem,
)
from agentgraph.core.state import WorkItem as CoreWorkItem
from agentgraph.infra import GitAdapter, GitCommitIdentity, GitRepository, ProcessRunner
from agentgraph.integration import SelectionDisposition, SelectionPlan, ShadowInputs
from agentgraph.integration.inspection import verify_work_source_revision
from agentgraph.runtime.codec import decode_value, sha256_digest
from agentgraph.work import (
    SelectionKind,
    WorkItemStatus,
    WorkSource,
    WorkSourceSnapshot,
)

from .analysis import AgentExecution
from .capability import capability_fingerprint, reconcile_write_capability
from .checkpoints import WriteCheckpointController
from .errors import WorkPlanMismatchError, WorkspaceError, WritePreparationError, WriteSliceError
from .evidence import read_evidence, write_evidence
from .models import WorkPlan, WorkPlanItem, WriteInputs, WriteRunInputs
from .provider import ChangeProvider
from .workspace import WriteExecution


def build_work_plan(
    source: WorkSource,
    snapshot: WorkSourceSnapshot,
    scope_id: str,
) -> WorkPlan:
    """Freeze every pending item package and its independently reconciled capability."""

    scope = source.get_scope(snapshot, scope_id)
    by_id = {item.item_id: item for item in snapshot.items}
    planned = []
    for item_id in scope.item_ids:
        item = by_id[item_id]
        if item.status is WorkItemStatus.COMPLETED:
            continue
        package = source.build_package(snapshot, item_id)
        selection = SelectionPlan(SelectionDisposition.READY, scope_id, item_id)
        allowed = reconcile_write_capability(snapshot, selection, package)
        if (
            package.item_id != item_id
            or package.scope_id != scope_id
            or package.parent_scope_id != scope.parent_scope_id
            or package.source_revision.fingerprint != snapshot.revision.fingerprint
            or package.branch_hint != scope.branch_hint
            or package.base_branch_hint != scope.base_branch_hint
        ):
            raise WorkPlanMismatchError("work_plan_mismatch")
        planned.append(
            WorkPlanItem(
                len(planned) + 1,
                item_id,
                package,
                sha256_digest(package),
                allowed,
                capability_fingerprint(allowed),
            )
        )
    return WorkPlan.create(scope_id, snapshot.revision.fingerprint, tuple(planned))


def projected_snapshot(
    snapshot: WorkSourceSnapshot, completed_item_ids: tuple[str, ...]
) -> WorkSourceSnapshot:
    """Overlay run-local completion without changing source revision or source data."""

    completed = frozenset(completed_item_ids)
    return replace(
        snapshot,
        items=tuple(
            replace(item, status=WorkItemStatus.COMPLETED) if item.item_id in completed else item
            for item in snapshot.items
        ),
    )


def item_root(run_path: Path, item: WorkPlanItem) -> Path:
    digest = hashlib.sha256(item.item_id.encode("utf-8")).hexdigest()[:12]
    return run_path / "items" / f"{item.plan_index:03d}-{digest}"


@dataclass(slots=True)
class MultiItemExecution:
    """Derive the current item execution solely from durable run authority."""

    run_inputs: WriteRunInputs
    plan: WorkPlan
    snapshot: WorkSourceSnapshot
    shadow: ShadowInputs
    source: WorkSource
    change_provider: ChangeProvider
    agent_provider: object
    review_agent_provider: object | None
    git: GitAdapter
    processes: ProcessRunner
    target: GitRepository
    run_id: str
    run_path: Path
    identity: GitCommitIdentity
    validation_timeout_seconds: float
    fault: object
    clock: object
    nonce_factory: object | None
    current: WriteExecution | None = None
    current_item_id: str | None = None

    def plan_item(self, item_id: str) -> WorkPlanItem:
        try:
            return next(item for item in self.plan.items if item.item_id == item_id)
        except StopIteration as exc:
            raise WritePreparationError("work_plan_mismatch") from exc

    def activate(self, state: GraphState, *, rehydrating: bool = False) -> WriteExecution:
        current = state.work.item
        if current is None:
            raise WritePreparationError("current_work_item_missing")
        if self.current is not None and self.current_item_id == current.id:
            return self.current
        item = self.plan_item(current.id)
        completed_ids = tuple(value.id for value in state.work.completed_items)
        if current.id in completed_ids or len(set(completed_ids)) != len(completed_ids):
            raise WritePreparationError("multi_item_lineage_mismatch")
        item_base, _commits = self._completed_lineage(completed_ids)
        root = item_root(self.run_path, item)
        evidence_root = self.run_path if self.run_inputs.max_work_items_per_run == 1 else root
        inputs = WriteInputs(
            project_id=self.run_inputs.project_id,
            package=item.package,
            expected_allowed_paths=item.allowed_paths,
            source_revision=self.run_inputs.source_revision,
            baseline_head=item_base,
            base_branch=(
                self.run_inputs.base_branch
                if item_base == self.run_inputs.target_baseline_head
                else self.run_inputs.scope_branch
            ),
            scope_branch=self.run_inputs.scope_branch,
            item_validation_checks=item.package.item_validation_checks,
            scope_required_checks=item.package.scope_required_checks,
            capability_fingerprint=item.capability_fingerprint,
            max_repair_cycles=self.run_inputs.max_repair_cycles,
            semantic_review_enabled=self.run_inputs.semantic_review_enabled,
            checkpoint_ttl_seconds=self.run_inputs.checkpoint_ttl_seconds,
            target_baseline_head=self.run_inputs.target_baseline_head,
            target_base_branch=self.run_inputs.base_branch,
            item_index=item.plan_index,
            work_plan_digest=self.plan.digest,
            run_inputs_digest=sha256_digest(self.run_inputs),
        )
        self._ensure_item_inputs(root, inputs)
        analysis_repository = self.target
        if completed_ids:
            analysis_repository = self._verified_workspace(item_base)
        analysis = AgentExecution(
            inputs,
            self.source,
            self.agent_provider,
            self.review_agent_provider if inputs.semantic_review_enabled else None,
            self.git,
            analysis_repository,
            self.run_id,
            evidence_root,
            source_root=self.target.root,
            guard_target=self.target,
        )
        execution = WriteExecution(
            self.shadow,
            inputs,
            self.source,
            self.change_provider,
            self.git,
            self.processes,
            self.target,
            self.run_id,
            self.run_path,
            analysis,
            self.identity,
            self.validation_timeout_seconds,
            rehydrating or item.plan_index > 1,
            self.fault,
            item_path=evidence_root,
        )
        if rehydrating:
            execution.rehydrate(state)
        self.current = execution
        self.current_item_id = current.id
        return execution

    def checkpoints(self, state: GraphState) -> WriteCheckpointController:
        return WriteCheckpointController(
            self.activate(state), clock=self.clock, nonce_factory=self.nonce_factory
        )

    def checkpoints_for_execution(self, execution: WriteExecution) -> WriteCheckpointController:
        return WriteCheckpointController(
            execution, clock=self.clock, nonce_factory=self.nonce_factory
        )

    def all_commit_shas(self, state: GraphState) -> tuple[str, ...]:
        return self._completed_lineage(tuple(item.id for item in state.work.completed_items))[1]

    def verify_run_boundary(self, state: GraphState) -> None:
        target = self.git.snapshot(self.target)
        if (
            target.head_sha != self.run_inputs.target_baseline_head
            or target.branch != self.run_inputs.base_branch
            or target.detached_head
            or target.dirty
            or target.conflicted_paths
        ):
            raise WorkspaceError("target_baseline_drift")
        snapshot = self.source.snapshot()
        try:
            verify_work_source_revision(self.target.root, snapshot.revision)
        except Exception as exc:
            raise WorkspaceError("work_source_drift") from exc
        if snapshot.revision.fingerprint != self.run_inputs.source_revision:
            raise WorkspaceError("work_source_drift")
        completed = tuple(item.id for item in state.work.completed_items)
        head, commits = self._completed_lineage(completed)
        if commits:
            self._verified_workspace(head)

    def _completed_lineage(self, completed_ids: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        head = self.run_inputs.target_baseline_head
        commits = []
        seen = set()
        for item_id in completed_ids:
            if item_id in seen:
                raise WritePreparationError("multi_item_lineage_mismatch")
            seen.add(item_id)
            item = self.plan_item(item_id)
            evidence_root = (
                self.run_path
                if self.run_inputs.max_work_items_per_run == 1
                else item_root(self.run_path, item)
            )
            document = read_evidence(evidence_root / "operations" / "commit.json")
            payload = document.get("payload")
            commit = payload.get("commit_sha") if isinstance(payload, dict) else None
            if (
                document.get("item_id") != item_id
                or document.get("item_base_head") != head
                or not isinstance(commit, str)
                or self.git.commit_parents(self.target, commit) != (head,)
            ):
                raise WritePreparationError("multi_item_lineage_mismatch")
            commits.append(commit)
            head = commit
        return head, tuple(commits)

    def _ensure_item_inputs(self, root: Path, inputs: WriteInputs) -> None:
        path = root / "write-inputs.json"
        if path.exists():
            document = read_evidence(path)
            restored = decode_value(document.get("payload"), WriteInputs)
            if restored != inputs:
                raise WritePreparationError("item_inputs_mismatch")
            return
        write_evidence(
            path,
            context={
                "project_id": inputs.project_id,
                "run_id": self.run_id,
                "item_id": inputs.package.item_id,
                "item_index": inputs.item_index,
                "item_base_head": inputs.baseline_head,
                "target_baseline_head": inputs.pinned_target_head,
                "work_plan_digest": inputs.work_plan_digest,
                "source_revision": inputs.source_revision,
                "capability_fingerprint": inputs.capability_fingerprint,
            },
            payload=inputs,
        )

    def _verified_workspace(self, item_base: str) -> GitRepository:
        workspace = self.run_path / "workspace"
        repository = self.git.discover_repository(workspace)
        snapshot = self.git.snapshot(repository)
        scope_head = self.git.resolve_ref(self.target, f"refs/heads/{self.run_inputs.scope_branch}")
        if (
            repository.root != workspace.resolve()
            or snapshot.branch != self.run_inputs.scope_branch
            or snapshot.head_sha != item_base
            or snapshot.dirty
            or snapshot.staged_paths
            or snapshot.conflicted_paths
            or scope_head != item_base
        ):
            raise WorkspaceError("multi_item_workspace_drift")
        return repository


@dataclass(frozen=True, slots=True)
class MultiItemSelectWorkNode:
    execution: MultiItemExecution
    node_id: str = "SELECT_WORK"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        completed = tuple(item.id for item in state.work.completed_items)
        projected = projected_snapshot(self.execution.snapshot, completed)
        selection = self.execution.source.next_ready_item(projected, self.execution.plan.scope_id)
        remaining = tuple(
            item for item in self.execution.plan.items if item.item_id not in completed
        )
        operations = [
            PatchOperation.set("work.source", type(self.execution.source).__name__),
            PatchOperation.set("work.hierarchy", self._hierarchy()),
            PatchOperation.set(
                "work.available_items", tuple(self._core_item(item.item_id) for item in remaining)
            ),
            PatchOperation.set("work.delivery_scope", self._delivery_scope()),
        ]
        if selection.kind is SelectionKind.READY:
            assert selection.item_id is not None
            plan_item = self.execution.plan_item(selection.item_id)
            operations.extend(
                (
                    PatchOperation.set("work.item", self._core_item(plan_item.item_id)),
                    PatchOperation.set("work.dependencies", plan_item.package.dependencies),
                )
            )
            self.execution.current = None
            self.execution.current_item_id = None
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.SUCCEEDED,
                state_patch=StatePatch(state.state_version, tuple(operations)),
            )
        if selection.kind in {SelectionKind.SCOPE_COMPLETE, SelectionKind.EMPTY_SCOPE}:
            operations.extend(
                (PatchOperation.clear("work.item"), PatchOperation.set("work.dependencies", ()))
            )
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.SUCCEEDED,
                state_patch=StatePatch(state.state_version, tuple(operations)),
            )
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.BLOCKED,
            reason=ResultReason(
                selection.reason_code or "blocked_item_dependencies",
                "remaining work items are dependency-blocked: "
                + ", ".join(selection.blocking_item_ids),
            ),
        )

    def _core_item(self, item_id: str) -> CoreWorkItem:
        item = self.execution.source.get_item(self.execution.snapshot, item_id)
        return CoreWorkItem(
            item.item_id,
            item.title,
            {
                "scope_id": item.scope_id,
                "risk": item.risk.value,
                "source_revision": self.execution.run_inputs.source_revision,
                "source_path": item.source_location.path,
                "source_line": item.source_location.line,
                "parallelizable": item.parallelizable,
                "final_review_required": item.final_review_required,
            },
        )

    def _hierarchy(self) -> tuple[WorkHierarchyItem, ...]:
        scope = self.execution.source.get_scope(
            self.execution.snapshot, self.execution.plan.scope_id
        )
        values = []
        if scope.parent_scope_id:
            parent = self.execution.source.get_scope(self.execution.snapshot, scope.parent_scope_id)
            values.append(
                WorkHierarchyItem(
                    "parent_scope",
                    parent.scope_id,
                    parent.title,
                    parent.status.value,
                    self.execution.run_inputs.source_revision,
                )
            )
        values.append(
            WorkHierarchyItem(
                "scope",
                scope.scope_id,
                scope.title,
                scope.status.value,
                self.execution.run_inputs.source_revision,
            )
        )
        return tuple(values)

    def _delivery_scope(self) -> DeliveryScope:
        paths = tuple(
            dict.fromkeys(
                path.path for item in self.execution.plan.items for path in item.allowed_paths
            )
        )
        inputs = self.execution.run_inputs
        return DeliveryScope(
            inputs.scope_id,
            paths,
            {
                "source_revision": inputs.source_revision,
                "work_plan_digest": self.execution.plan.digest,
                "target_baseline_head": inputs.target_baseline_head,
                "scope_branch": inputs.scope_branch,
            },
        )


@dataclass(frozen=True, slots=True)
class DynamicItemNode:
    execution: MultiItemExecution
    node_id: str
    factory: Callable[[WriteExecution], object]

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        try:
            item_execution = self.execution.activate(state)
        except WriteSliceError as exc:
            code = getattr(exc, "code", None) or str(exc) or "item_activation_failed"
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.BLOCKED,
                reason=ResultReason(code, str(exc)),
            )
        node = self.factory(item_execution)
        return node.run(state, context)
