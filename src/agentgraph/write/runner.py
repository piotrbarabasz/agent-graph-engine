"""Durable orchestration for the first controlled local write slice."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from agentgraph.core import CommitMode, GraphEngine, PolicySnapshot, RunStatus, canonical_v1_graph
from agentgraph.infra import GitAdapter, GitCommitIdentity, ProcessRunner
from agentgraph.infra.errors import GitError, NotAGitRepositoryError
from agentgraph.integration import (
    BranchDisposition,
    ShadowInputs,
    ShadowRequest,
    ShadowSelectionError,
    WorkSourceRepositoryMismatchError,
    assess_preflight,
    inspect_project,
    prepare_selection,
)
from agentgraph.nodes import (
    AssessRiskNode,
    BuildTaskPackageNode,
    ClassifyFailureNode,
    CloseTaskNode,
    DiscoverProjectNode,
    ExploreNode,
    FinalizeNode,
    ImplementNode,
    MoreWorkNode,
    PreflightNode,
    ReviewNode,
    SelectWorkNode,
    StartNode,
    ValidateNode,
)
from agentgraph.runtime import (
    DurableGraphCoordinator,
    ProjectRegistry,
    RecoveryAssessment,
    RuntimePaths,
)
from agentgraph.runtime.ids import generate_run_id
from agentgraph.work import InvalidWorkSourceError, WorkRisk, WorkScopeStatus, WorkSource

from .capability import capability_fingerprint, reconcile_write_capability
from .errors import WorkCapabilityMismatchError, WritePreparationError
from .models import (
    WriteInputs,
    WriteSliceIssue,
    WriteSliceOutcome,
    WriteSliceReport,
    WriteSliceRequest,
)
from .provider import ChangeProvider
from .workspace import WriteExecution


class WriteSliceRunner:
    """Prepare pinned inputs, then execute exactly one item with zero repairs."""

    def __init__(
        self,
        repository_root: Path | str,
        work_source: WorkSource,
        change_provider: ChangeProvider,
        *,
        git_adapter: GitAdapter,
        project_registry: ProjectRegistry,
        process_runner: ProcessRunner | None = None,
        runtime_paths: RuntimePaths | None = None,
        commit_identity: GitCommitIdentity | None = None,
        run_id_factory: Callable[[], str] = generate_run_id,
        fault: Callable[[str], None] | None = None,
        validation_timeout_seconds: float = 120.0,
        max_steps: int = 30,
    ) -> None:
        if validation_timeout_seconds <= 0 or max_steps < 1:
            raise ValueError("write runner bounds must be positive")
        self.repository_root = Path(repository_root)
        self.work_source = work_source
        self.change_provider = change_provider
        self.git = git_adapter
        self.registry = project_registry
        self.paths = runtime_paths or project_registry.paths
        if self.paths.root != project_registry.paths.root:
            raise WritePreparationError("runtime paths must match the project registry")
        self.processes = process_runner or git_adapter.runner
        self.identity = commit_identity or GitCommitIdentity("AgentGraph", "agentgraph@localhost")
        self.run_id_factory = run_id_factory
        self.fault = fault
        self.validation_timeout_seconds = validation_timeout_seconds
        self.max_steps = max_steps
        self._coordinator: DurableGraphCoordinator | None = None
        self._last_run_id: str | None = None

    def run(self, request: WriteSliceRequest | None = None) -> WriteSliceReport:
        request = request or WriteSliceRequest()
        inspection = None
        try:
            inspection = inspect_project(
                self.repository_root,
                git_adapter=self.git,
                project_registry=self.registry,
                work_source=self.work_source,
            )
            selection, package = prepare_selection(
                self.work_source,
                inspection.work_snapshot,
                ShadowRequest(request.scope_id, request.parent_scope_id),
            )
        except WorkSourceRepositoryMismatchError as exc:
            return self._early(
                WriteSliceOutcome.INVALID_SOURCE, "work_source_repository_mismatch", str(exc)
            )
        except (InvalidWorkSourceError, ShadowSelectionError) as exc:
            return self._early(WriteSliceOutcome.INVALID_SOURCE, "work_package_mismatch", str(exc))
        except (NotAGitRepositoryError, GitError) as exc:
            return self._early(WriteSliceOutcome.BLOCKED, "git_repository_invalid", str(exc))

        assert inspection is not None
        if package is None or selection.item_id is None or selection.scope_id is None:
            code = selection.issues[0].code if selection.issues else selection.reason_code
            return self._early(
                WriteSliceOutcome.BLOCKED,
                code or "write_selection_not_ready",
                "one explicit READY work item is required",
                inspection=inspection,
            )
        scope = next(
            value
            for value in inspection.work_snapshot.scopes
            if value.scope_id == selection.scope_id
        )
        if scope.status is not WorkScopeStatus.PLANNED:
            return self._early(
                WriteSliceOutcome.BLOCKED,
                "active_scope_write_not_supported_in_m006",
                "M006 accepts PLANNED scopes only",
                inspection=inspection,
                selection=selection,
            )
        if package.risk is WorkRisk.CRITICAL:
            return self._early(
                WriteSliceOutcome.BLOCKED,
                "critical_risk_not_supported_in_m006",
                "M006 does not execute critical-risk work",
                inspection=inspection,
                selection=selection,
            )
        if not scope.branch_hint or not scope.base_branch_hint:
            return self._early(
                WriteSliceOutcome.INVALID_SOURCE,
                "write_branch_hint_missing",
                "planned write scope requires branch and base-branch hints",
                inspection=inspection,
                selection=selection,
            )
        preflight = assess_preflight(inspection, selection)
        if (
            not preflight.ready
            or preflight.branch_disposition is not BranchDisposition.PREPARATION_REQUIRED
        ):
            code = preflight.issues[0].code if preflight.issues else "write_preflight_not_ready"
            return self._early(
                WriteSliceOutcome.BLOCKED,
                code,
                "planned scope must start from its clean base branch",
                inspection=inspection,
                selection=selection,
            )
        try:
            allowed = reconcile_write_capability(inspection.work_snapshot, selection, package)
        except WorkCapabilityMismatchError as exc:
            return self._early(
                WriteSliceOutcome.INVALID_SOURCE,
                "work_package_capability_mismatch",
                str(exc),
                inspection=inspection,
                selection=selection,
            )
        if self.git.local_branch_exists(inspection.repository, scope.branch_hint):
            return self._early(
                WriteSliceOutcome.BLOCKED,
                "scope_branch_already_exists",
                "scope branch already exists locally",
                inspection=inspection,
                selection=selection,
            )

        fingerprint = _input_fingerprint(inspection, request)
        shadow = ShadowInputs(inspection, preflight, selection, package, fingerprint)
        assert inspection.git_snapshot.head_sha is not None
        inputs = WriteInputs(
            inspection.project_id,
            package,
            allowed,
            inspection.work_snapshot.revision.fingerprint,
            inspection.git_snapshot.head_sha,
            scope.base_branch_hint,
            scope.branch_hint,
            package.item_validation_checks,
            package.scope_required_checks,
            capability_fingerprint(allowed),
        )
        run_id = self.run_id_factory()
        self._last_run_id = run_id
        run_path = self.paths.run(inspection.project_id, run_id)
        execution = WriteExecution(
            shadow,
            inputs,
            self.work_source,
            self.change_provider,
            self.git,
            self.processes,
            inspection.repository,
            run_id,
            run_path,
            self.identity,
            self.validation_timeout_seconds,
        )
        nodes = {
            "START": StartNode(),
            "DISCOVER_PROJECT": DiscoverProjectNode(shadow, shadow=False),
            "PREFLIGHT": PreflightNode(shadow, shadow=False),
            "SELECT_WORK": SelectWorkNode(shadow),
            "EXPLORE": ExploreNode(inputs),
            "BUILD_TASK_PACKAGE": BuildTaskPackageNode(inputs),
            "ASSESS_RISK": AssessRiskNode(inputs),
            "IMPLEMENT": ImplementNode(execution),
            "VALIDATE": ValidateNode(execution),
            "REVIEW": ReviewNode(execution),
            "CLASSIFY_FAILURE": ClassifyFailureNode(),
            "CLOSE_TASK": CloseTaskNode(execution),
            "MORE_WORK": MoreWorkNode(),
            "FINALIZE": FinalizeNode(),
        }
        policy = PolicySnapshot(
            max_repair_cycles=0,
            max_work_items_per_run=1,
            commit_mode=CommitMode.PER_WORK_ITEM,
        )
        engine = GraphEngine(canonical_v1_graph(), policy, nodes)
        coordinator = DurableGraphCoordinator(
            self.paths,
            self.registry.get(inspection.project_id),
            engine,
            run_id_factory=lambda: run_id,
            fault=self.fault,
        )
        self._coordinator = coordinator
        coordinator.start_run(run_id)
        executed: list[str] = []
        with coordinator.open_session(run_id) as session:
            state = session.store.load()
            for _ in range(self.max_steps):
                if state.graph.current_node == "END":
                    break
                executed.append(state.graph.current_node)
                state = session.step()
            else:
                raise WritePreparationError("write graph exceeded its bounded step count")
        outcome = (
            WriteSliceOutcome.LOCAL_COMMIT_CREATED
            if execution.commit_sha is not None and state.run.status is RunStatus.PAUSED
            else WriteSliceOutcome.BLOCKED
            if state.run.status is RunStatus.BLOCKED
            else WriteSliceOutcome.FAILED
        )
        issue = ()
        if outcome is not WriteSliceOutcome.LOCAL_COMMIT_CREATED:
            issue = (
                WriteSliceIssue(
                    state.failure.code or "write_run_not_completed",
                    f"write run finalized with {state.run.status.value}",
                ),
            )
        return WriteSliceReport(
            outcome,
            inspection.project_id,
            run_id,
            state,
            selection.item_id,
            selection.scope_id,
            inputs.source_revision,
            inputs.baseline_head,
            inputs.scope_branch,
            execution.commit_sha,
            str(run_path),
            str(execution.workspace) if execution.workspace.exists() else None,
            tuple(executed),
            issue,
            None if execution.changeset is None else execution.changeset.digest,
            ()
            if execution.applied is None
            else tuple(item.path for item in execution.applied.files),
        )

    def assess_recovery(self, run_id: str | None = None) -> RecoveryAssessment:
        coordinator = self._coordinator
        selected = run_id or self._last_run_id
        if coordinator is None or selected is None:
            raise WritePreparationError("runner has no interrupted durable run")
        with coordinator.open_session(selected, recovery=True) as session:
            return session.assess_recovery()

    def _early(
        self,
        outcome: WriteSliceOutcome,
        code: str,
        message: str,
        *,
        inspection=None,
        selection=None,
    ) -> WriteSliceReport:
        return WriteSliceReport(
            outcome,
            None if inspection is None else inspection.project_id,
            None,
            None,
            None if selection is None else selection.item_id,
            None if selection is None else selection.scope_id,
            None if inspection is None else inspection.work_snapshot.revision.fingerprint,
            None if inspection is None else inspection.git_snapshot.head_sha,
            None,
            None,
            None,
            None,
            issues=(WriteSliceIssue(code, message),),
        )


def _input_fingerprint(inspection, request: WriteSliceRequest) -> str:
    payload = {
        "project_id": inspection.project_id,
        "head_sha": inspection.git_snapshot.head_sha,
        "branch": inspection.git_snapshot.branch,
        "work_source_revision": inspection.work_snapshot.revision.fingerprint,
        "scope_id": request.scope_id,
        "parent_scope_id": request.parent_scope_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"
