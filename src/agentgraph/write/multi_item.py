"""Frozen-plan, sequential per-item execution for one durable scope run."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
from .errors import (
    ItemInputsMismatchError,
    MultiItemEvidenceError,
    MultiItemLineageError,
    WorkPlanMismatchError,
    WorkspaceError,
    WritePreparationError,
    WriteSliceError,
)
from .evidence import read_evidence, write_evidence
from .item_storage import verify_item_storage
from .models import (
    AppliedChangeSet,
    ChangeSet,
    CommitWitness,
    CompletedItemReport,
    WorkPlan,
    WorkPlanItem,
    WorkspaceManifest,
    WriteInputs,
    WriteRunInputs,
)
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


def item_inputs(
    run: WriteRunInputs, plan: WorkPlan, item: WorkPlanItem, item_base_head: str
) -> WriteInputs:
    """Reconstruct one item's immutable authority from frozen run inputs."""

    return WriteInputs(
        project_id=run.project_id,
        package=item.package,
        expected_allowed_paths=item.allowed_paths,
        source_revision=run.source_revision,
        baseline_head=item_base_head,
        base_branch=run.base_branch
        if item_base_head == run.target_baseline_head
        else run.scope_branch,
        scope_branch=run.scope_branch,
        item_validation_checks=item.package.item_validation_checks,
        scope_required_checks=item.package.scope_required_checks,
        capability_fingerprint=item.capability_fingerprint,
        max_repair_cycles=run.max_repair_cycles,
        semantic_review_enabled=run.semantic_review_enabled,
        checkpoint_ttl_seconds=run.checkpoint_ttl_seconds,
        target_baseline_head=run.target_baseline_head,
        target_base_branch=run.base_branch,
        item_index=item.plan_index,
        work_plan_digest=plan.digest,
        run_inputs_digest=sha256_digest(run),
    )


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
    verified_completed: tuple[CompletedItemReport, ...] = field(default=(), init=False)

    def plan_item(self, item_id: str) -> WorkPlanItem:
        try:
            return next(item for item in self.plan.items if item.item_id == item_id)
        except StopIteration as exc:
            raise WritePreparationError("work_plan_mismatch") from exc

    def activate(self, state: GraphState, *, rehydrating: bool = False) -> WriteExecution:
        current = state.work.item
        if current is None:
            raise WritePreparationError("current_work_item_missing")
        item = self.plan_item(current.id)
        completed_ids = tuple(value.id for value in state.work.completed_items)
        if current.id in completed_ids or len(set(completed_ids)) != len(completed_ids):
            raise MultiItemLineageError("multi_item_lineage_mismatch")
        selection = self.source.next_ready_item(
            projected_snapshot(self.snapshot, completed_ids), self.plan.scope_id
        )
        if selection.kind is not SelectionKind.READY or selection.item_id != current.id:
            raise MultiItemLineageError("multi_item_lineage_mismatch")
        root = item_root(self.run_path, item)
        evidence_root = self.run_path if self.run_inputs.max_work_items_per_run == 1 else root
        verify_item_storage(self.run_path, root, evidence_root)
        if self.current is not None and self.current_item_id == current.id:
            return self.current
        item_base, _commits = self._completed_lineage(completed_ids)
        inputs = item_inputs(self.run_inputs, self.plan, item, item_base)
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
            rehydrating or bool(completed_ids),
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
        return tuple(item.commit_sha for item in self.completed_reports(state))

    def completed_reports(self, state: GraphState) -> tuple[CompletedItemReport, ...]:
        completed = tuple(item.id for item in state.work.completed_items)
        self._completed_lineage(completed)
        return self.verified_completed

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
        verified: list[CompletedItemReport] = []
        replayed: list[str] = []
        projected = self.snapshot
        self.verified_completed = ()
        for item_id in completed_ids:
            if item_id in replayed:
                raise MultiItemLineageError("multi_item_lineage_mismatch")
            selection = self.source.next_ready_item(projected, self.plan.scope_id)
            if selection.kind is not SelectionKind.READY or selection.item_id != item_id:
                raise MultiItemLineageError("multi_item_lineage_mismatch")
            item = self.plan_item(item_id)
            report = self.verify_completed_item(item, head)
            verified.append(report)
            self.verified_completed = tuple(verified)
            head = report.commit_sha
            replayed.append(item_id)
            projected = projected_snapshot(self.snapshot, tuple(replayed))
        return head, tuple(item.commit_sha for item in verified)

    def verify_completed_item(
        self, plan_item: WorkPlanItem, expected_item_base: str
    ) -> CompletedItemReport:
        """Verify one completed item from frozen inputs through the Git object database."""

        root = item_root(self.run_path, plan_item)
        evidence_root = self.run_path if self.run_inputs.max_work_items_per_run == 1 else root
        inputs_path = root / "write-inputs.json"
        verify_item_storage(self.run_path, root, evidence_root, inputs_path)
        expected_inputs = item_inputs(self.run_inputs, self.plan, plan_item, expected_item_base)
        try:
            inputs_document = read_evidence(inputs_path)
            restored_inputs = decode_value(inputs_document.get("payload"), WriteInputs)
        except Exception as exc:
            raise ItemInputsMismatchError("item_inputs_mismatch") from exc
        expected_context = {
            "project_id": expected_inputs.project_id,
            "run_id": self.run_id,
            "item_id": plan_item.item_id,
            "item_index": plan_item.plan_index,
            "item_base_head": expected_item_base,
            "target_baseline_head": self.run_inputs.target_baseline_head,
            "work_plan_digest": self.plan.digest,
            "source_revision": self.run_inputs.source_revision,
            "capability_fingerprint": plan_item.capability_fingerprint,
        }
        if restored_inputs != expected_inputs or any(
            inputs_document.get(key) != value for key, value in expected_context.items()
        ):
            raise ItemInputsMismatchError("item_inputs_mismatch")

        operations = evidence_root / "operations"
        witness_document = self._operation_document(
            operations / "commit-witness.json", root, evidence_root, expected_inputs
        )
        try:
            witness = decode_value(witness_document.get("payload"), CommitWitness)
        except Exception as exc:
            raise MultiItemEvidenceError("multi_item_evidence_mismatch") from exc
        if (
            witness.project_id != expected_inputs.project_id
            or witness.run_id != self.run_id
            or witness.item_id != plan_item.item_id
            or witness.scope_id != plan_item.package.scope_id
            or witness.base_head != expected_item_base
            or witness.previous_branch_head != expected_item_base
            or not isinstance(witness.commit_sha, str)
            or type(witness.repair_count) is not int
            or not 0 <= witness.repair_count <= self.run_inputs.max_repair_cycles
            or not witness.changeset_digest
            or not witness.workspace_manifest_digest
        ):
            raise MultiItemEvidenceError("multi_item_evidence_mismatch")

        cycle = witness.repair_count
        cycle_root = operations if cycle == 0 else operations / "repairs" / f"{cycle:03d}"
        manifest_document = self._operation_document(
            cycle_root / "workspace-manifest.json", root, evidence_root, expected_inputs
        )
        try:
            manifest_payload = manifest_document.get("payload")
            if not isinstance(manifest_payload, dict):
                raise ValueError("manifest payload")
            manifest = decode_value(manifest_payload["manifest"], WorkspaceManifest)
        except Exception as exc:
            raise MultiItemEvidenceError("multi_item_evidence_mismatch") from exc
        changed_paths = tuple(entry.path for entry in manifest.files)
        if (
            manifest.baseline_head != expected_item_base
            or manifest.cycle != cycle
            or manifest.digest != witness.workspace_manifest_digest
            or changed_paths != witness.reviewed_paths
        ):
            raise MultiItemEvidenceError("multi_item_evidence_mismatch")

        proposal_name = "implement-proposal.json" if cycle == 0 else "proposal.json"
        applied_name = "implement-applied.json" if cycle == 0 else "applied.json"
        proposal_document = self._operation_document(
            cycle_root / proposal_name, root, evidence_root, expected_inputs
        )
        applied_document = self._operation_document(
            cycle_root / applied_name, root, evidence_root, expected_inputs
        )
        try:
            proposal_payload = proposal_document.get("payload")
            applied_payload = applied_document.get("payload")
            if not isinstance(proposal_payload, dict) or not isinstance(applied_payload, dict):
                raise ValueError("changeset payload")
            changeset = decode_value(proposal_payload["changeset"], ChangeSet)
            applied = decode_value(applied_payload["applied"], AppliedChangeSet)
        except Exception as exc:
            raise MultiItemEvidenceError("multi_item_evidence_mismatch") from exc
        if (
            changeset.digest != witness.changeset_digest
            or applied.changeset_digest != changeset.digest
            or tuple(file.path for file in applied.files)
            != tuple(change.path for change in changeset.changes)
        ):
            raise MultiItemEvidenceError("multi_item_evidence_mismatch")

        analysis = AgentExecution(
            expected_inputs,
            self.source,
            self.agent_provider,
            self.review_agent_provider if expected_inputs.semantic_review_enabled else None,
            self.git,
            self.target,
            self.run_id,
            evidence_root,
            source_root=self.target.root,
            guard_target=self.target,
        )
        verifier = WriteExecution(
            self.shadow,
            expected_inputs,
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
            True,
            self.fault,
            item_path=evidence_root,
        )
        try:
            review_paths = [
                cycle_root / "validation.json",
                cycle_root / "review.json",
                cycle_root / "mechanical-review.json",
            ]
            if expected_inputs.semantic_review_enabled:
                review_paths.append(cycle_root / "review-context.json")
            review_documents = [
                self._operation_document(path, root, evidence_root, expected_inputs)
                for path in review_paths
            ]
            if expected_inputs.semantic_review_enabled:
                review_payload = review_documents[1].get("payload")
                semantic = (
                    review_payload.get("semantic") if isinstance(review_payload, dict) else None
                )
                reference = (
                    semantic.get("evidence_reference") if isinstance(semantic, dict) else None
                )
                if not isinstance(reference, str):
                    raise MultiItemEvidenceError("multi_item_evidence_mismatch")
                agent_evidence = evidence_root.joinpath(*reference.split("/"))
                verify_item_storage(self.run_path, root, evidence_root, agent_evidence)
            verifier.verify_completed_review(cycle, manifest)
        except Exception as exc:
            if isinstance(exc, MultiItemEvidenceError):
                raise
            raise MultiItemEvidenceError("multi_item_evidence_mismatch") from exc

        commit_document = self._operation_document(
            operations / "commit.json", root, evidence_root, expected_inputs
        )
        commit_payload = commit_document.get("payload")
        receipt_commit = (
            commit_payload.get("commit_sha") if isinstance(commit_payload, dict) else None
        )
        if receipt_commit != witness.commit_sha:
            raise MultiItemEvidenceError("multi_item_evidence_mismatch")
        try:
            if self.git.resolve_ref(self.target, witness.commit_sha) != witness.commit_sha:
                raise MultiItemLineageError("multi_item_lineage_mismatch")
            if self.git.commit_parents(self.target, witness.commit_sha) != (expected_item_base,):
                raise MultiItemLineageError("multi_item_lineage_mismatch")
            committed_paths = tuple(
                path.as_posix()
                for path in self.git.diff_paths_between(
                    self.target, expected_item_base, witness.commit_sha
                )
            )
            if set(committed_paths) != set(changed_paths) or len(committed_paths) != len(
                changed_paths
            ):
                raise MultiItemEvidenceError("multi_item_evidence_mismatch")
            for entry in manifest.files:
                tree_entry = self.git.tree_entry(self.target, witness.commit_sha, entry.path)
                expected_mode = "100755" if entry.mode == 0o755 else "100644"
                if (
                    tree_entry is None
                    or tree_entry.object_type != "blob"
                    or tree_entry.mode != expected_mode
                    or hashlib.sha256(
                        self.git.read_blob(self.target, tree_entry.object_id)
                    ).hexdigest()
                    != entry.sha256
                ):
                    raise MultiItemEvidenceError("multi_item_evidence_mismatch")
        except (MultiItemLineageError, MultiItemEvidenceError):
            raise
        except Exception as exc:
            raise MultiItemLineageError("multi_item_lineage_mismatch") from exc
        return CompletedItemReport(
            plan_item.item_id,
            plan_item.plan_index,
            expected_item_base,
            witness.commit_sha,
            changeset.digest,
            manifest.digest,
            changed_paths,
            cycle,
        )

    def _operation_document(
        self,
        path: Path,
        root: Path,
        evidence_root: Path,
        inputs: WriteInputs,
    ) -> dict[str, object]:
        verify_item_storage(self.run_path, root, evidence_root, path)
        try:
            document = read_evidence(path)
        except Exception as exc:
            raise MultiItemEvidenceError("multi_item_evidence_mismatch") from exc
        expected = {
            "project_id": inputs.project_id,
            "run_id": self.run_id,
            "item_id": inputs.package.item_id,
            "scope_id": inputs.package.scope_id,
            "item_base_head": inputs.baseline_head,
            "target_baseline_head": inputs.pinned_target_head,
            "item_index": inputs.item_index,
            "work_plan_digest": inputs.work_plan_digest,
            "source_revision": inputs.source_revision,
            "capability_fingerprint": inputs.capability_fingerprint,
        }
        if any(document.get(key) != value for key, value in expected.items()):
            raise MultiItemEvidenceError("multi_item_evidence_mismatch")
        return document

    def _ensure_item_inputs(self, root: Path, inputs: WriteInputs) -> None:
        path = root / "write-inputs.json"
        evidence_root = self.run_path if self.run_inputs.max_work_items_per_run == 1 else root
        verify_item_storage(self.run_path, root, evidence_root, path)
        if path.exists():
            try:
                document = read_evidence(path)
                restored = decode_value(document.get("payload"), WriteInputs)
            except Exception as exc:
                raise ItemInputsMismatchError("item_inputs_mismatch") from exc
            expected_context = {
                "project_id": inputs.project_id,
                "run_id": self.run_id,
                "item_id": inputs.package.item_id,
                "item_index": inputs.item_index,
                "item_base_head": inputs.baseline_head,
                "target_baseline_head": inputs.pinned_target_head,
                "work_plan_digest": inputs.work_plan_digest,
                "source_revision": inputs.source_revision,
                "capability_fingerprint": inputs.capability_fingerprint,
            }
            if restored != inputs or any(
                document.get(key) != value for key, value in expected_context.items()
            ):
                raise ItemInputsMismatchError("item_inputs_mismatch")
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
