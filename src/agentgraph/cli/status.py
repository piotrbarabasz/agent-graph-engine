"""Strictly read-only durable run and checkpoint inspection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentgraph.config import ExecutionProfile
from agentgraph.runtime import (
    CheckpointRequestRecord,
    CheckpointStore,
    ProjectRegistry,
    RuntimePaths,
)
from agentgraph.runtime.codec import decode_value, parse_json_bytes, parse_timestamp, sha256_digest
from agentgraph.runtime.ids import validate_run_id
from agentgraph.runtime.lifecycle import ActiveRunRecord
from agentgraph.runtime.state_store import StateStore
from agentgraph.write import (
    CommitWitness,
    PublishCheckpointRequestRecord,
    PublishPlan,
    WorkPlan,
    WriteInputs,
    WriteRunInputs,
    write_run_inputs_digest,
)
from agentgraph.write.evidence import read_evidence
from agentgraph.write.item_storage import verify_item_storage
from agentgraph.write.multi_item import item_root as work_plan_item_root
from agentgraph.write.publish_history import (
    read_local_publish_history,
    verify_publish_checkpoint_plan_binding,
)
from agentgraph.write.publish_storage import load_typed

from .errors import CliError
from .models import CliIssue, SafeCheckpoint, SafePublish


@dataclass(frozen=True, slots=True)
class StatusReport:
    project_id: str
    run_id: str
    run_status: str
    current_node: str
    pending_resume_node: str | None
    scope_id: str
    scope_branch: str
    baseline_head: str
    commit_sha: str | None
    completed_item_ids: tuple[str, ...]
    checkpoint: SafeCheckpoint | None
    publish: SafePublish | None
    issues: tuple[CliIssue, ...]
    profile_digest: str | None
    profile_bound: bool
    profile_match: bool | None


@dataclass(frozen=True, slots=True)
class MaterializedCheckpoint:
    view: SafeCheckpoint
    nonce: str


class StatusService:
    """Read existing evidence without graph execution, recovery, Git, or remote access."""

    def __init__(
        self,
        repository_root: Path,
        paths: RuntimePaths,
        registry: ProjectRegistry,
        current_profile: ExecutionProfile,
    ) -> None:
        self.repository_root = repository_root
        self.paths = paths
        self.registry = registry
        self.current_profile = current_profile

    def resolve_run_id(self, run_id: str | None) -> tuple[str, str]:
        validated = None
        if run_id is not None:
            try:
                validated = validate_run_id(run_id)
            except Exception as exc:
                raise CliError("invalid_run_id", "run ID is invalid") from exc
        project = self.registry.find_by_root(self.repository_root)
        if project is None:
            raise CliError("active_run_not_found", "this project has no durable runs")
        if validated is not None:
            return project.project_id, validated
        path = self.paths.active_run(project.project_id)
        try:
            record = decode_value(parse_json_bytes(path.read_bytes()), ActiveRunRecord)
        except FileNotFoundError as exc:
            raise CliError("active_run_not_found", "this project has no active run") from exc
        except Exception as exc:
            raise CliError("active_run_invalid", "active-run authority is invalid") from exc
        if record.project_id != project.project_id:
            raise CliError("active_run_invalid", "active run belongs to another project")
        return project.project_id, record.run_id

    def inspect(self, run_id: str | None = None) -> StatusReport:
        project_id, selected = self.resolve_run_id(run_id)
        run_path = self.paths.run(project_id, selected)
        try:
            run_document = read_evidence(run_path / "run-inputs.json")
            run_inputs = decode_value(run_document.get("payload"), WriteRunInputs)
            state = StateStore(run_path / "state.json").load()
            if (
                run_document.get("project_id") != project_id
                or run_document.get("run_id") != selected
                or run_inputs.project_id != project_id
                or state.run.run_id != selected
            ):
                raise ValueError("run identity mismatch")
            profile_match = self._profile_match(run_path, run_inputs, project_id, selected)
            completed_ids = tuple(item.id for item in state.work.completed_items)
            commits = self._completed_commits(run_path, project_id, selected, completed_ids)
            checkpoint = self._checkpoint(run_path, state, run_inputs)
            issues: tuple[CliIssue, ...] = ()
            try:
                publish = self._publish(run_path, run_inputs, state, completed_ids, commits)
            except Exception as exc:
                publish = None
                detail = getattr(exc, "code", None) or str(exc) or type(exc).__name__
                issues = (
                    CliIssue(
                        "publish_evidence_mismatch",
                        f"durable publication evidence is invalid: {detail}",
                    ),
                )
            return StatusReport(
                project_id,
                selected,
                state.run.status.value,
                state.graph.current_node,
                state.graph.pending_resume_node,
                run_inputs.scope_id,
                run_inputs.scope_branch,
                run_inputs.target_baseline_head,
                commits[-1] if commits else None,
                completed_ids,
                checkpoint,
                publish,
                issues,
                run_inputs.execution_profile_digest,
                run_inputs.execution_profile_digest is not None,
                profile_match,
            )
        except CliError:
            raise
        except Exception as exc:
            raise CliError("run_evidence_invalid", "durable run evidence is invalid") from exc

    @staticmethod
    def _completed_commits(
        run_path: Path,
        project_id: str,
        run_id: str,
        completed_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not completed_ids:
            return ()
        items_root = run_path / "items"
        witnesses: dict[str, str] = {}
        for item_root in items_root.iterdir():
            if not item_root.is_dir() or item_root.is_symlink():
                continue
            path = item_root / "operations" / "commit-witness.json"
            if not path.exists():
                continue
            document = read_evidence(path)
            witness = decode_value(document.get("payload"), CommitWitness)
            if (
                document.get("project_id") != project_id
                or document.get("run_id") != run_id
                or witness.project_id != project_id
                or witness.run_id != run_id
                or witness.commit_sha is None
                or witness.item_id in witnesses
            ):
                raise ValueError("completed item witness is invalid")
            witnesses[witness.item_id] = witness.commit_sha
        if set(witnesses) != set(completed_ids):
            raise ValueError("completed item witness set is incomplete")
        return tuple(witnesses[item_id] for item_id in completed_ids)

    def materialized_checkpoint(self, run_id: str | None = None) -> MaterializedCheckpoint:
        project_id, selected = self.resolve_run_id(run_id)
        run_path = self.paths.run(project_id, selected)
        try:
            run_document = read_evidence(run_path / "run-inputs.json")
            run_inputs = decode_value(run_document.get("payload"), WriteRunInputs)
            state = StateStore(run_path / "state.json").load()
            if (
                run_document.get("project_id") != project_id
                or run_document.get("run_id") != selected
                or run_inputs.project_id != project_id
                or state.run.run_id != selected
            ):
                raise CliError("run_evidence_invalid", "durable run identity is invalid")
            if (
                state.graph.current_node != "HUMAN_CHECKPOINT"
                or not state.graph.pending_resume_node
            ):
                raise CliError("checkpoint_not_pending", "run has no pending checkpoint")
            checkpoint_id = self._checkpoint_id(
                state.state_version, state.graph.pending_resume_node
            )
            store = CheckpointStore(run_path)
            if state.graph.pending_resume_node == "CREATE_PR":
                request = store.load_typed_request(checkpoint_id, PublishCheckpointRequestRecord)
            else:
                request = store.load_request(checkpoint_id)
            if request is None:
                raise CliError(
                    "checkpoint_not_materialized",
                    f"checkpoint is not materialized; run agentgraph resume {selected}",
                )
            self._validate_current_checkpoint(run_path, request, state, run_inputs)
            view = self._safe_checkpoint(run_path, request, run_inputs, state)
            return MaterializedCheckpoint(view, request.nonce)
        except CliError:
            raise
        except Exception as exc:
            raise CliError("checkpoint_evidence_invalid", "checkpoint evidence is invalid") from exc

    def _profile_match(
        self,
        run_path: Path,
        inputs: WriteRunInputs,
        project_id: str,
        run_id: str,
    ) -> bool | None:
        expected = inputs.execution_profile_digest
        if expected is None:
            return None
        try:
            document = read_evidence(run_path / "execution-profile.json")
            profile = decode_value(document.get("payload"), ExecutionProfile)
        except Exception as exc:
            raise CliError(
                "execution_profile_invalid", "execution profile evidence is invalid"
            ) from exc
        if (
            document.get("project_id") != project_id
            or document.get("run_id") != run_id
            or document.get("execution_profile_digest") != expected
            or profile.digest != expected
        ):
            raise CliError("execution_profile_invalid", "execution profile binding is invalid")
        return self.current_profile.digest == expected

    def _checkpoint(self, run_path, state, inputs) -> SafeCheckpoint | None:
        if state.graph.current_node != "HUMAN_CHECKPOINT" or not state.graph.pending_resume_node:
            return None
        checkpoint_id = self._checkpoint_id(state.state_version, state.graph.pending_resume_node)
        store = CheckpointStore(run_path)
        if state.graph.pending_resume_node == "CREATE_PR":
            request = store.load_typed_request(checkpoint_id, PublishCheckpointRequestRecord)
        else:
            request = store.load_request(checkpoint_id)
        if request is None:
            return None
        self._validate_current_checkpoint(run_path, request, state, inputs)
        return self._safe_checkpoint(run_path, request, inputs, state)

    def _safe_checkpoint(self, run_path, request, inputs, state) -> SafeCheckpoint:
        if isinstance(request, PublishCheckpointRequestRecord):
            context = {
                "project_id": inputs.project_id,
                "run_id": state.run.run_id,
                "scope_id": inputs.scope_id,
            }
            plan = load_typed(run_path, "plan.json", PublishPlan, context)
            if plan is None:
                raise CliError("checkpoint_binding_mismatch", "publish plan is missing")
            try:
                verify_publish_checkpoint_plan_binding(request, plan)
            except Exception as exc:
                raise CliError(
                    "checkpoint_binding_mismatch",
                    "publish checkpoint no longer binds its publication plan",
                ) from exc
            return SafeCheckpoint(
                request.checkpoint_id,
                "publish",
                request.code,
                request.message,
                request.created_at,
                request.expires_at,
                request.pending_resume_node,
                scope_id=inputs.scope_id,
                repository=request.remote_repository_full_name,
                base_branch=request.base_branch,
                head_branch=request.remote_head_branch,
                final_head=request.final_head,
                draft=request.draft,
                pr_title=plan.pr_title,
                publish_plan_digest=request.publish_plan_digest,
            )
        if not isinstance(request, CheckpointRequestRecord):
            raise ValueError("unsupported checkpoint request")
        return SafeCheckpoint(
            request.checkpoint_id,
            "item_risk",
            request.code,
            request.message,
            request.created_at,
            request.expires_at,
            request.pending_resume_node,
            scope_id=inputs.scope_id,
            item_id=None if state.work.item is None else state.work.item.id,
        )

    def _publish(
        self,
        run_path: Path,
        inputs: WriteRunInputs,
        state,
        completed_item_ids: tuple[str, ...],
        completed_commit_shas: tuple[str, ...],
    ) -> SafePublish | None:
        report = read_local_publish_history(
            run_path,
            inputs,
            state,
            completed_item_ids,
            completed_commit_shas,
        )
        if report is None:
            return None
        return SafePublish(
            report.remote_repository_full_name,
            report.base_branch,
            report.head_branch,
            report.head_sha,
            report.pr_number,
            report.pr_url,
            report.draft,
            report.publish_plan_digest,
        )

    def _validate_current_checkpoint(self, run_path, request, state, inputs) -> None:
        item_inputs = None
        if not isinstance(request, PublishCheckpointRequestRecord):
            item_inputs = self._current_item_inputs(run_path, state, inputs)
        source_revision = (
            inputs.source_revision if item_inputs is None else item_inputs.source_revision
        )
        expected = {
            "checkpoint_id": self._checkpoint_id(
                state.state_version, state.graph.pending_resume_node
            ),
            "project_id": inputs.project_id,
            "run_id": state.run.run_id,
            "node_id": "HUMAN_CHECKPOINT",
            "pending_resume_node": state.graph.pending_resume_node,
            "state_version": state.state_version,
            "state_digest": StateStore.digest_for_state(state),
            "source_revision": source_revision,
        }
        if any(getattr(request, name, None) != value for name, value in expected.items()):
            raise CliError(
                "checkpoint_binding_mismatch",
                "checkpoint request does not bind the current paused state",
            )
        if isinstance(request, PublishCheckpointRequestRecord):
            lifetime = parse_timestamp(request.expires_at) - parse_timestamp(request.created_at)
            if lifetime.total_seconds() != inputs.checkpoint_ttl_seconds:
                raise CliError(
                    "checkpoint_binding_mismatch", "checkpoint lifetime authority is invalid"
                )
            if (
                request.target_baseline_head != inputs.target_baseline_head
                or request.work_plan_digest != inputs.work_plan_digest
            ):
                raise CliError(
                    "checkpoint_binding_mismatch", "publish checkpoint authority is invalid"
                )
            return
        if item_inputs is None:
            raise CliError("checkpoint_binding_mismatch", "item checkpoint authority is missing")
        lifetime = parse_timestamp(request.expires_at) - parse_timestamp(request.created_at)
        operations_digest = sha256_digest(
            {
                "changes": state.changes,
                "validation": state.validation,
                "review": state.review,
                "repair": state.repair,
                "commits": state.commits,
                "push": state.push,
                "pull_request": state.pull_request,
            }
        )
        if (
            lifetime.total_seconds() != item_inputs.checkpoint_ttl_seconds
            or request.project_id != item_inputs.project_id
            or request.package_digest != sha256_digest(item_inputs.package)
            or request.write_inputs_digest != sha256_digest(item_inputs)
            or request.baseline_head != item_inputs.baseline_head
            or request.capability_fingerprint != item_inputs.capability_fingerprint
            or request.risk_level != state.risk.level
            or request.operations_digest != operations_digest
        ):
            raise CliError("checkpoint_binding_mismatch", "item checkpoint authority is invalid")

    @staticmethod
    def _current_item_inputs(run_path: Path, state, run_inputs: WriteRunInputs) -> WriteInputs:
        try:
            plan_document = read_evidence(run_path / "work-plan.json")
            plan = decode_value(plan_document.get("payload"), WorkPlan)
            if (
                plan_document.get("project_id") != run_inputs.project_id
                or plan_document.get("run_id") != state.run.run_id
                or plan.digest != run_inputs.work_plan_digest
                or plan.scope_id != run_inputs.scope_id
                or plan.source_revision != run_inputs.source_revision
                or state.work.item is None
            ):
                raise ValueError("work plan does not bind the current item")
            planned = next(
                (item for item in plan.items if item.item_id == state.work.item.id),
                None,
            )
            if planned is None:
                raise ValueError("current item is absent from the work plan")
            root = work_plan_item_root(run_path, planned)
            evidence_root = run_path if run_inputs.max_work_items_per_run == 1 else root
            inputs_path = root / "write-inputs.json"
            verify_item_storage(run_path, root, evidence_root, inputs_path)
            inputs_document = read_evidence(inputs_path)
            item_inputs = decode_value(inputs_document.get("payload"), WriteInputs)
            context = {
                "project_id": item_inputs.project_id,
                "run_id": state.run.run_id,
                "item_id": item_inputs.package.item_id,
                "item_index": item_inputs.item_index,
                "item_base_head": item_inputs.baseline_head,
                "target_baseline_head": item_inputs.pinned_target_head,
                "work_plan_digest": item_inputs.work_plan_digest,
                "source_revision": item_inputs.source_revision,
                "capability_fingerprint": item_inputs.capability_fingerprint,
            }
            if any(inputs_document.get(name) != value for name, value in context.items()):
                raise ValueError("item input evidence context is invalid")
            expected_base_branch = (
                run_inputs.base_branch
                if item_inputs.baseline_head == run_inputs.target_baseline_head
                else run_inputs.scope_branch
            )
            if (
                item_inputs.project_id != run_inputs.project_id
                or item_inputs.package != planned.package
                or item_inputs.expected_allowed_paths != planned.allowed_paths
                or item_inputs.source_revision != run_inputs.source_revision
                or item_inputs.base_branch != expected_base_branch
                or item_inputs.scope_branch != run_inputs.scope_branch
                or item_inputs.item_validation_checks != planned.package.item_validation_checks
                or item_inputs.scope_required_checks != planned.package.scope_required_checks
                or item_inputs.capability_fingerprint != planned.capability_fingerprint
                or item_inputs.max_repair_cycles != run_inputs.max_repair_cycles
                or item_inputs.semantic_review_enabled != run_inputs.semantic_review_enabled
                or item_inputs.checkpoint_ttl_seconds != run_inputs.checkpoint_ttl_seconds
                or item_inputs.target_baseline_head != run_inputs.target_baseline_head
                or item_inputs.target_base_branch != run_inputs.base_branch
                or item_inputs.item_index != planned.plan_index
                or item_inputs.work_plan_digest != run_inputs.work_plan_digest
                or item_inputs.run_inputs_digest != write_run_inputs_digest(run_inputs)
            ):
                raise ValueError("item input authority is invalid")
            return item_inputs
        except CliError:
            raise
        except Exception as exc:
            raise CliError(
                "checkpoint_evidence_invalid",
                "current item checkpoint evidence is invalid",
            ) from exc

    @staticmethod
    def _checkpoint_id(state_version: int, pending_resume_node: str) -> str:
        suffix = "create_pr" if pending_resume_node == "CREATE_PR" else pending_resume_node.lower()
        return f"checkpoint-{state_version}-{suffix}"
