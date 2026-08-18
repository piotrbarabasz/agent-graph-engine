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
from agentgraph.runtime.codec import decode_value, parse_json_bytes
from agentgraph.runtime.ids import validate_run_id
from agentgraph.runtime.lifecycle import ActiveRunRecord
from agentgraph.runtime.state_store import StateStore
from agentgraph.write import (
    CommitWitness,
    PublishCheckpointRequestRecord,
    PublishPlan,
    PublishResult,
    WriteRunInputs,
)
from agentgraph.write.evidence import read_evidence

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
            checkpoint = self._checkpoint(run_path, state, run_inputs)
            publish = self._publish(run_path, run_inputs)
            completed_ids = tuple(item.id for item in state.work.completed_items)
            commits = self._completed_commits(run_path, project_id, selected, completed_ids)
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
                (),
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
            view = self._safe_checkpoint(run_path, request, run_inputs)
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
        return None if request is None else self._safe_checkpoint(run_path, request, inputs)

    def _safe_checkpoint(self, run_path, request, inputs) -> SafeCheckpoint:
        if isinstance(request, PublishCheckpointRequestRecord):
            plan_doc = read_evidence(run_path / "publish" / "plan.json")
            plan = decode_value(plan_doc.get("payload"), PublishPlan)
            if plan.digest != request.publish_plan_digest:
                raise ValueError("publish plan mismatch")
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
        )

    def _publish(self, run_path: Path, inputs: WriteRunInputs) -> SafePublish | None:
        path = run_path / "publish" / "result.json"
        if not path.exists():
            return None
        document = read_evidence(path)
        result = decode_value(document.get("payload"), PublishResult)
        if result.project_id != inputs.project_id or result.scope_id != inputs.scope_id:
            raise ValueError("publish result identity mismatch")
        plan_document = read_evidence(run_path / "publish" / "plan.json")
        plan = decode_value(plan_document.get("payload"), PublishPlan)
        if result.publish_plan_digest != plan.digest:
            raise ValueError("publish result plan mismatch")
        return SafePublish(
            result.remote_repository_full_name,
            plan.base_branch,
            result.remote_branch,
            result.final_head,
            result.pr_number,
            result.pr_url,
            result.draft,
            result.publish_plan_digest,
        )

    @staticmethod
    def _checkpoint_id(state_version: int, pending_resume_node: str) -> str:
        suffix = "create_pr" if pending_resume_node == "CREATE_PR" else pending_resume_node.lower()
        return f"checkpoint-{state_version}-{suffix}"
