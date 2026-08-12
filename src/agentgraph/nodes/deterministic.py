"""Deterministic effect-free nodes for the read-only graph probe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentgraph.core import (
    DeliveryScope,
    Evidence,
    NodeContext,
    NodeResult,
    NodeStatus,
    PatchOperation,
    ResultReason,
    StatePatch,
    WorkHierarchyItem,
)
from agentgraph.core.state import GraphState
from agentgraph.core.state import WorkItem as CoreWorkItem
from agentgraph.work import WorkItem as SourceWorkItem
from agentgraph.work import WorkItemStatus

if TYPE_CHECKING:
    from agentgraph.integration.models import ShadowInputs


@dataclass(frozen=True, slots=True)
class StartNode:
    node_id: str = "START"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        del state
        return NodeResult(self.node_id, context.node_attempt_id, NodeStatus.SUCCEEDED)


@dataclass(frozen=True, slots=True)
class DiscoverProjectNode:
    inputs: ShadowInputs
    node_id: str = "DISCOVER_PROJECT"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        inspection = self.inputs.inspection
        git = inspection.git_snapshot
        revision = inspection.work_snapshot.revision.fingerprint
        operations = (
            PatchOperation.set("repository.identifier", inspection.project_id),
            PatchOperation.set(
                "repository.metadata",
                {
                    "head_sha": git.head_sha,
                    "branch": git.branch,
                    "detached_head": git.detached_head,
                    "dirty": git.dirty,
                    "upstream": git.upstream,
                    "work_source_revision": revision,
                    "shadow": True,
                },
            ),
            PatchOperation.set("project.name", inspection.project_name),
            PatchOperation.set(
                "project.metadata",
                {
                    "project_id": inspection.project_id,
                    "work_source_kind": inspection.work_source_kind,
                    "shadow": True,
                },
            ),
            PatchOperation.set("baseline.revision", git.head_sha),
            PatchOperation.set(
                "baseline.metadata",
                {
                    "work_source_revision": revision,
                    "shadow_input_fingerprint": self.inputs.input_fingerprint,
                },
            ),
        )
        evidence = [Evidence("work_source_revision", revision)]
        if git.head_sha is not None:
            evidence.insert(0, Evidence("repository_revision", git.head_sha))
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            state_patch=StatePatch(state.state_version, operations),
            evidence=tuple(evidence),
        )


@dataclass(frozen=True, slots=True)
class PreflightNode:
    inputs: ShadowInputs
    node_id: str = "PREFLIGHT"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        assessment = self.inputs.preflight
        if not assessment.ready:
            issue = assessment.primary_issue
            assert issue is not None
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.BLOCKED,
                reason=ResultReason(issue.code, issue.message),
            )
        revision = self.inputs.inspection.work_snapshot.revision.fingerprint
        head = self.inputs.inspection.git_snapshot.head_sha
        operations = (
            PatchOperation.set(
                "architecture_invariants.items",
                (
                    "shadow_read_only",
                    "work_source_snapshot_pinned",
                    "repository_baseline_pinned",
                    "no_llm_execution",
                    "no_target_mutation",
                ),
            ),
            PatchOperation.set(
                "requirements.items",
                (f"source_revision:{revision}", f"head_sha:{head}"),
            ),
        )
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            state_patch=StatePatch(state.state_version, operations),
        )


@dataclass(frozen=True, slots=True)
class SelectWorkNode:
    inputs: ShadowInputs
    node_id: str = "SELECT_WORK"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        selection = self.inputs.selection
        if selection.disposition.value in {"blocked", "selection_required"}:
            issue = selection.issues[0]
            return NodeResult(
                self.node_id,
                context.node_attempt_id,
                NodeStatus.BLOCKED,
                reason=ResultReason(issue.code, issue.message),
            )

        operations = list(self._common_operations())
        if selection.disposition.value == "no_work":
            operations.extend(
                (
                    PatchOperation.clear("work.item"),
                    PatchOperation.set("work.dependencies", ()),
                    PatchOperation.clear("work.delivery_scope"),
                    PatchOperation.set("work.available_items", ()),
                )
            )
        else:
            package = self.inputs.work_package
            assert package is not None
            item = self._core_item(
                next(
                    candidate
                    for candidate in self.inputs.inspection.work_snapshot.items
                    if candidate.item_id == package.item_id
                )
            )
            available = tuple(
                self._core_item(candidate)
                for candidate in self._scope_items(package.scope_id)
                if candidate.status is WorkItemStatus.PENDING
            )
            operations.extend(
                (
                    PatchOperation.set("work.item", item),
                    PatchOperation.set("work.dependencies", package.dependencies),
                    PatchOperation.set(
                        "work.delivery_scope",
                        DeliveryScope(
                            package.scope_id,
                            tuple(path.path for path in package.allowed_paths),
                            {"source_revision": package.source_revision.fingerprint},
                        ),
                    ),
                    PatchOperation.set("work.available_items", available),
                )
            )
        return NodeResult(
            self.node_id,
            context.node_attempt_id,
            NodeStatus.SUCCEEDED,
            state_patch=StatePatch(state.state_version, tuple(operations)),
        )

    def _common_operations(self) -> tuple[PatchOperation, ...]:
        return (
            PatchOperation.set("work.source", self.inputs.inspection.work_source_kind),
            PatchOperation.set("work.hierarchy", self._hierarchy()),
        )

    def _hierarchy(self) -> tuple[WorkHierarchyItem, ...]:
        scope_id = self.inputs.selection.scope_id
        if scope_id is None:
            return ()
        snapshot = self.inputs.inspection.work_snapshot
        scope = next(item for item in snapshot.scopes if item.scope_id == scope_id)
        values = []
        if scope.parent_scope_id is not None:
            parent = next(
                item for item in snapshot.scopes if item.scope_id == scope.parent_scope_id
            )
            values.append(
                WorkHierarchyItem(
                    "parent_scope",
                    parent.scope_id,
                    parent.title,
                    parent.status.value,
                    snapshot.revision.fingerprint,
                )
            )
        values.append(
            WorkHierarchyItem(
                "scope",
                scope.scope_id,
                scope.title,
                scope.status.value,
                snapshot.revision.fingerprint,
            )
        )
        return tuple(values)

    def _scope_items(self, scope_id: str) -> tuple[SourceWorkItem, ...]:
        snapshot = self.inputs.inspection.work_snapshot
        by_id = {item.item_id: item for item in snapshot.items}
        scope = next(item for item in snapshot.scopes if item.scope_id == scope_id)
        return tuple(by_id[item_id] for item_id in scope.item_ids)

    def _core_item(self, item: SourceWorkItem) -> CoreWorkItem:
        return CoreWorkItem(
            item.item_id,
            item.title,
            {
                "scope_id": item.scope_id,
                "risk": item.risk.value,
                "source_revision": self.inputs.inspection.work_snapshot.revision.fingerprint,
                "source_path": item.source_location.path,
                "source_line": item.source_location.line,
                "parallelizable": item.parallelizable,
                "final_review_required": item.final_review_required,
            },
        )


@dataclass(frozen=True, slots=True)
class FinalizeNode:
    node_id: str = "FINALIZE"

    def run(self, state: GraphState, context: NodeContext) -> NodeResult:
        del state
        return NodeResult(self.node_id, context.node_attempt_id, NodeStatus.SUCCEEDED)
