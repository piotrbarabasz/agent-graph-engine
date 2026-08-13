"""Durable orchestration for the first controlled local write slice."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from agentgraph.agents import AgentProvider, DeclaredWorkAgentProvider
from agentgraph.core import (
    CheckpointOutcome,
    CommitMode,
    GraphEngine,
    PolicySnapshot,
    RepairClassification,
    RunStatus,
    canonical_v1_graph,
)
from agentgraph.infra import GitAdapter, GitCommitIdentity, ProcessRunner
from agentgraph.infra.errors import GitError, NotAGitRepositoryError
from agentgraph.integration import (
    BranchDisposition,
    RepositoryRootMismatchError,
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
    HumanCheckpointNode,
    ImplementNode,
    MoreWorkNode,
    PreflightNode,
    RepairNode,
    ReviewNode,
    SelectWorkNode,
    StartNode,
    ValidateNode,
)
from agentgraph.runtime import (
    CheckpointDecision,
    DurableGraphCoordinator,
    ProjectRegistry,
    RecoveryAction,
    RecoveryAssessment,
    RuntimePaths,
    StateStore,
)
from agentgraph.runtime.codec import decode_value, utc_now
from agentgraph.runtime.ids import generate_run_id
from agentgraph.work import InvalidWorkSourceError, WorkScopeStatus, WorkSource

from .analysis import AgentExecution
from .capability import capability_fingerprint, reconcile_write_capability
from .checkpoints import WriteCheckpointController
from .errors import (
    CheckpointError,
    RepairPolicyError,
    ReviewProviderRequiredError,
    WorkCapabilityMismatchError,
    WorkspaceError,
    WritePreparationError,
)
from .evidence import read_evidence, write_evidence
from .models import (
    CheckpointView,
    WriteInputs,
    WriteSliceIssue,
    WriteSliceOutcome,
    WriteSliceReport,
    WriteSliceRequest,
)
from .provider import ChangeProvider
from .workspace import WriteExecution


class WriteSliceRunner:
    """Prepare pinned inputs, then execute one item with bounded graph repairs."""

    def __init__(
        self,
        repository_root: Path | str,
        work_source: WorkSource,
        change_provider: ChangeProvider,
        *,
        agent_provider: AgentProvider | None = None,
        review_agent_provider: AgentProvider | None = None,
        git_adapter: GitAdapter,
        project_registry: ProjectRegistry,
        process_runner: ProcessRunner | None = None,
        runtime_paths: RuntimePaths | None = None,
        commit_identity: GitCommitIdentity | None = None,
        run_id_factory: Callable[[], str] = generate_run_id,
        fault: Callable[[str], None] | None = None,
        validation_timeout_seconds: float = 120.0,
        max_steps: int = 30,
        max_repair_cycles: int = 0,
        checkpoint_ttl_seconds: int = 3600,
        clock: Callable[[], datetime] = utc_now,
        checkpoint_nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if validation_timeout_seconds <= 0 or max_steps < 1:
            raise ValueError("write runner bounds must be positive")
        if type(max_repair_cycles) is not int or max_repair_cycles not in {0, 1, 2}:
            raise RepairPolicyError("M009 supports max_repair_cycles values 0, 1, or 2")
        if type(checkpoint_ttl_seconds) is not int or not 60 <= checkpoint_ttl_seconds <= 86400:
            raise ValueError("checkpoint TTL must be between 60 and 86400 seconds")
        self.repository_root = Path(repository_root)
        self.work_source = work_source
        self.change_provider = change_provider
        self.agent_provider = agent_provider or DeclaredWorkAgentProvider()
        self.review_agent_provider = review_agent_provider
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
        self.max_repair_cycles = max_repair_cycles
        self.checkpoint_ttl_seconds = checkpoint_ttl_seconds
        self.clock = clock
        self.checkpoint_nonce_factory = checkpoint_nonce_factory
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
        except RepositoryRootMismatchError as exc:
            return self._early(WriteSliceOutcome.BLOCKED, "repository_root_mismatch", str(exc))
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
            project_id=inspection.project_id,
            package=package,
            expected_allowed_paths=allowed,
            source_revision=inspection.work_snapshot.revision.fingerprint,
            baseline_head=inspection.git_snapshot.head_sha,
            base_branch=scope.base_branch_hint,
            scope_branch=scope.branch_hint,
            item_validation_checks=package.item_validation_checks,
            scope_required_checks=package.scope_required_checks,
            capability_fingerprint=capability_fingerprint(allowed),
            max_repair_cycles=self.max_repair_cycles,
            semantic_review_enabled=self.review_agent_provider is not None,
            checkpoint_ttl_seconds=self.checkpoint_ttl_seconds,
        )
        run_id = self.run_id_factory()
        self._last_run_id = run_id
        execution, coordinator, checkpoints = self._build_runtime(
            inspection, shadow, inputs, run_id, rehydrating=False
        )
        self._coordinator = coordinator
        coordinator.start_run(
            run_id,
            initialize_artifacts=lambda staging: write_evidence(
                staging / "write-inputs.json",
                context={
                    "project_id": inputs.project_id,
                    "run_id": run_id,
                    "item_id": inputs.package.item_id,
                    "scope_id": inputs.package.scope_id,
                    "pinned_head": inputs.baseline_head,
                    "source_revision": inputs.source_revision,
                },
                payload=inputs,
            ),
        )
        executed: list[str] = []
        checkpoint: CheckpointView | None = None
        with coordinator.open_session(run_id) as session:
            state = session.store.load()
            for _ in range(self.max_steps):
                if state.graph.current_node == "END":
                    break
                executed.append(state.graph.current_node)
                state = session.step()
                if state.graph.current_node == "HUMAN_CHECKPOINT":
                    try:
                        checkpoint = checkpoints.view(checkpoints.ensure_request(state))
                    except CheckpointError as exc:
                        return self._checkpoint_recovery(state, execution, exc)
                    if self.fault is not None:
                        self.fault("CHECKPOINT_REQUEST_PERSISTED")
                    break
            else:
                raise WritePreparationError("write graph exceeded its bounded step count")
        return self._report(state, execution, tuple(executed), checkpoint=checkpoint)

    def resume(self, run_id: str) -> WriteSliceReport:
        """Reconstruct a durable run from immutable inputs and continue only when safe."""

        try:
            execution, coordinator, checkpoints = self._existing_runtime(run_id)
        except (
            WritePreparationError,
            WorkspaceError,
            RepositoryRootMismatchError,
            WorkSourceRepositoryMismatchError,
        ) as exc:
            pending = self._pending_checkpoint_report(run_id, "checkpoint_binding_mismatch")
            if pending is not None:
                return pending
            return self._early(
                WriteSliceOutcome.RECOVERY_REQUIRED,
                getattr(exc, "code", None) or "write_resume_inputs_invalid",
                str(exc),
            )
        self._coordinator = coordinator
        self._last_run_id = run_id
        executed: list[str] = []
        with coordinator.open_session(run_id, recovery=True) as session:
            assessment = session.assess_recovery()
            if assessment.action is RecoveryAction.BLOCKED:
                state = session.store.load()
                try:
                    execution.rehydrate(state)
                except WorkspaceError as exc:
                    return self._rehydration_required(state, execution, exc)
                return self._recovery_required(state, execution, assessment)
            if assessment.action in {
                RecoveryAction.REAPPLY_RECORDED_RESULT,
                RecoveryAction.COMPLETE_TRANSITION_MARKER,
            }:
                assessment = session.recover()
                if assessment.action is RecoveryAction.BLOCKED:
                    state = session.store.load()
                    try:
                        execution.rehydrate(state)
                    except WorkspaceError as exc:
                        return self._rehydration_required(state, execution, exc)
                    return self._recovery_required(state, execution, assessment)
            state = session.store.load()
            try:
                execution.rehydrate(state)
            except WorkspaceError as exc:
                return self._rehydration_required(state, execution, exc)
            if assessment.action is RecoveryAction.COMPLETED:
                return self._report(state, execution, ())
            if assessment.action not in {
                RecoveryAction.CLEAN_RESUME,
                RecoveryAction.RERUN_INTERRUPTED_NODE,
            }:
                return self._recovery_required(state, execution, assessment)
            if state.graph.current_node == "HUMAN_CHECKPOINT":
                try:
                    request, decision = checkpoints.decision(state)
                except CheckpointError as exc:
                    return self._checkpoint_recovery(state, execution, exc)
                if decision is None and not checkpoints.expired(request):
                    return self._report(
                        state,
                        execution,
                        (),
                        checkpoint=checkpoints.view(request),
                    )
            for _ in range(self.max_steps):
                if state.graph.current_node == "END":
                    break
                executed.append(state.graph.current_node)
                state = session.step()
                if state.graph.current_node == "HUMAN_CHECKPOINT":
                    try:
                        request, decision = checkpoints.decision(state)
                    except CheckpointError as exc:
                        return self._checkpoint_recovery(state, execution, exc)
                    if decision is None and not checkpoints.expired(request):
                        return self._report(
                            state,
                            execution,
                            tuple(executed),
                            checkpoint=checkpoints.view(request),
                        )
            else:
                raise WritePreparationError("resumed write graph exceeded its step bound")
        return self._report(state, execution, tuple(executed))

    def assess_recovery(self, run_id: str | None = None) -> RecoveryAssessment:
        selected = run_id or self._last_run_id
        if selected is None:
            raise WritePreparationError("a durable run ID is required")
        _, coordinator, _ = self._existing_runtime(selected)
        with coordinator.open_session(selected, recovery=True) as session:
            return session.assess_recovery()

    def submit_checkpoint(
        self,
        run_id: str,
        *,
        checkpoint_id: str,
        nonce: str,
        outcome: CheckpointOutcome,
        actor: str,
    ) -> CheckpointDecision:
        """Persist one immutable decision; execution resumes only via resume()."""

        try:
            _, coordinator, checkpoints = self._existing_runtime(run_id)
        except (
            WritePreparationError,
            WorkspaceError,
            RepositoryRootMismatchError,
            WorkSourceRepositoryMismatchError,
        ) as exc:
            if self._pending_checkpoint_report(run_id, "checkpoint_binding_mismatch") is not None:
                raise CheckpointError("checkpoint_binding_mismatch") from exc
            raise
        with coordinator.open_session(run_id, recovery=True) as session:
            assessment = session.assess_recovery()
            if assessment.action in {
                RecoveryAction.REAPPLY_RECORDED_RESULT,
                RecoveryAction.COMPLETE_TRANSITION_MARKER,
            }:
                assessment = session.recover()
            if assessment.action not in {
                RecoveryAction.CLEAN_RESUME,
                RecoveryAction.RERUN_INTERRUPTED_NODE,
            }:
                raise CheckpointError("checkpoint_not_pending")
            return checkpoints.submit(
                session.store.load(),
                checkpoint_id=checkpoint_id,
                nonce=nonce,
                outcome=outcome,
                actor=actor,
            )

    def _existing_runtime(
        self, run_id: str
    ) -> tuple[WriteExecution, DurableGraphCoordinator, WriteCheckpointController]:
        configured = self.repository_root.expanduser().resolve()
        repository = self.git.discover_repository(configured)
        if repository.root != configured:
            raise RepositoryRootMismatchError("configured target must equal the canonical Git root")
        project = self.registry.find_by_root(repository.root)
        if project is None:
            raise WritePreparationError("target repository is absent from the runtime registry")
        run_path = self.paths.run(project.project_id, run_id)
        document = read_evidence(run_path / "write-inputs.json")
        if document.get("project_id") != project.project_id or document.get("run_id") != run_id:
            raise WritePreparationError("write-inputs identity differs from requested run")
        inputs = decode_value(document.get("payload"), WriteInputs)
        if inputs.semantic_review_enabled and self.review_agent_provider is None:
            raise ReviewProviderRequiredError(
                "semantic review is enabled for this run but no review provider was supplied"
            )
        inspection = inspect_project(
            repository.root,
            git_adapter=self.git,
            project_registry=self.registry,
            work_source=self.work_source,
        )
        selection, package = prepare_selection(
            self.work_source,
            inspection.work_snapshot,
            ShadowRequest(scope_id=inputs.package.scope_id),
        )
        if package is None:
            raise WritePreparationError("persisted selection is no longer reconstructable")
        allowed = reconcile_write_capability(inspection.work_snapshot, selection, package)
        reconstructed = WriteInputs(
            project_id=inspection.project_id,
            package=package,
            expected_allowed_paths=allowed,
            source_revision=inspection.work_snapshot.revision.fingerprint,
            baseline_head=inputs.baseline_head,
            base_branch=inputs.base_branch,
            scope_branch=inputs.scope_branch,
            item_validation_checks=package.item_validation_checks,
            scope_required_checks=package.scope_required_checks,
            capability_fingerprint=capability_fingerprint(allowed),
            max_repair_cycles=inputs.max_repair_cycles,
            semantic_review_enabled=inputs.semantic_review_enabled,
            checkpoint_ttl_seconds=inputs.checkpoint_ttl_seconds,
        )
        if reconstructed != inputs:
            raise WritePreparationError("live source differs from persisted write inputs")
        if inputs.max_repair_cycles != self.max_repair_cycles:
            raise RepairPolicyError("resume repair policy differs from persisted write inputs")
        preflight = assess_preflight(inspection, selection)
        shadow = ShadowInputs(
            inspection,
            preflight,
            selection,
            package,
            _input_fingerprint(inspection, WriteSliceRequest(scope_id=package.scope_id)),
        )
        return self._build_runtime(inspection, shadow, inputs, run_id, rehydrating=True)

    def _build_runtime(
        self,
        inspection,
        shadow: ShadowInputs,
        inputs: WriteInputs,
        run_id: str,
        *,
        rehydrating: bool,
    ) -> tuple[WriteExecution, DurableGraphCoordinator, WriteCheckpointController]:
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
            AgentExecution(
                inputs,
                self.work_source,
                self.agent_provider,
                self.review_agent_provider if inputs.semantic_review_enabled else None,
                self.git,
                inspection.repository,
                run_id,
                run_path,
            ),
            self.identity,
            self.validation_timeout_seconds,
            rehydrating,
            self.fault or (lambda stage: None),
        )
        checkpoints = WriteCheckpointController(
            execution,
            clock=self.clock,
            nonce_factory=self.checkpoint_nonce_factory,
        )
        nodes = {
            "START": StartNode(),
            "DISCOVER_PROJECT": DiscoverProjectNode(shadow, shadow=False),
            "PREFLIGHT": PreflightNode(shadow, shadow=False),
            "SELECT_WORK": SelectWorkNode(shadow),
            "EXPLORE": ExploreNode(inputs, execution.analysis),
            "BUILD_TASK_PACKAGE": BuildTaskPackageNode(inputs, execution.analysis),
            "ASSESS_RISK": AssessRiskNode(inputs, execution.analysis),
            "HUMAN_CHECKPOINT": HumanCheckpointNode(checkpoints),
            "IMPLEMENT": ImplementNode(execution),
            "VALIDATE": ValidateNode(execution),
            "REVIEW": ReviewNode(execution),
            "CLASSIFY_FAILURE": ClassifyFailureNode(execution),
            "PROGRAMMER_REPAIR": RepairNode(
                execution, RepairClassification.PROGRAMMER, "PROGRAMMER_REPAIR"
            ),
            "DEBUGGER": RepairNode(execution, RepairClassification.DEBUGGER, "DEBUGGER"),
            "CLOSE_TASK": CloseTaskNode(execution),
            "MORE_WORK": MoreWorkNode(),
            "FINALIZE": FinalizeNode(),
        }
        policy = PolicySnapshot(
            max_repair_cycles=inputs.max_repair_cycles,
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
        return execution, coordinator, checkpoints

    def _report(
        self,
        state,
        execution: WriteExecution,
        executed: tuple[str, ...],
        *,
        checkpoint: CheckpointView | None = None,
    ) -> WriteSliceReport:
        outcome = (
            WriteSliceOutcome.CHECKPOINT_REQUIRED
            if checkpoint is not None
            else WriteSliceOutcome.LOCAL_COMMIT_CREATED
            if execution.commit_sha is not None and state.run.status is RunStatus.PAUSED
            else WriteSliceOutcome.BLOCKED
            if state.run.status is RunStatus.BLOCKED
            else WriteSliceOutcome.FAILED
        )
        issues = ()
        if outcome not in {
            WriteSliceOutcome.LOCAL_COMMIT_CREATED,
            WriteSliceOutcome.CHECKPOINT_REQUIRED,
        }:
            issues = (
                WriteSliceIssue(
                    execution.issue_code
                    or execution.analysis.issue_code
                    or state.failure.code
                    or "write_run_not_completed",
                    f"write run finalized with {state.run.status.value}",
                ),
            )
        inputs = execution.inputs
        return WriteSliceReport(
            outcome,
            inputs.project_id,
            execution.run_id,
            state,
            inputs.package.item_id,
            inputs.package.scope_id,
            inputs.source_revision,
            inputs.baseline_head,
            inputs.scope_branch,
            execution.commit_sha,
            str(execution.run_path),
            str(execution.workspace) if execution.workspace.exists() else None,
            executed,
            issues,
            None if execution.changeset is None else execution.changeset.digest,
            ()
            if execution.manifest is None
            else tuple(item.path for item in execution.manifest.files),
            checkpoint,
        )

    def _checkpoint_recovery(
        self, state, execution: WriteExecution, error: CheckpointError
    ) -> WriteSliceReport:
        report = self._report(state, execution, ())
        return WriteSliceReport(
            outcome=WriteSliceOutcome.RECOVERY_REQUIRED,
            project_id=report.project_id,
            run_id=report.run_id,
            graph_state=report.graph_state,
            selected_item_id=report.selected_item_id,
            selected_scope_id=report.selected_scope_id,
            source_revision=report.source_revision,
            baseline_head=report.baseline_head,
            scope_branch=report.scope_branch,
            commit_sha=report.commit_sha,
            runtime_path=report.runtime_path,
            workspace_path=report.workspace_path,
            issues=(WriteSliceIssue(error.code, str(error)),),
            changeset_digest=report.changeset_digest,
            changed_paths=report.changed_paths,
        )

    def _pending_checkpoint_report(self, run_id: str, code: str) -> WriteSliceReport | None:
        """Report fail-closed drift even when live inputs cannot be reconstructed."""

        try:
            configured = self.repository_root.expanduser().resolve()
            repository = self.git.discover_repository(configured)
            project = self.registry.find_by_root(repository.root)
            if project is None:
                return None
            run_path = self.paths.run(project.project_id, run_id)
            document = read_evidence(run_path / "write-inputs.json")
            inputs = decode_value(document.get("payload"), WriteInputs)
            state = StateStore(run_path / "state.json").load()
            if (
                state.run.status is not RunStatus.RUNNING
                or state.graph.current_node != "HUMAN_CHECKPOINT"
            ):
                return None
            return WriteSliceReport(
                outcome=WriteSliceOutcome.RECOVERY_REQUIRED,
                project_id=inputs.project_id,
                run_id=run_id,
                graph_state=state,
                selected_item_id=inputs.package.item_id,
                selected_scope_id=inputs.package.scope_id,
                source_revision=inputs.source_revision,
                baseline_head=inputs.baseline_head,
                scope_branch=inputs.scope_branch,
                commit_sha=None,
                runtime_path=str(run_path),
                workspace_path=None,
                issues=(WriteSliceIssue(code, "checkpoint binding no longer matches"),),
            )
        except Exception:
            return None

    def _recovery_required(
        self, state, execution: WriteExecution, assessment: RecoveryAssessment
    ) -> WriteSliceReport:
        report = self._report(state, execution, ())
        return WriteSliceReport(
            WriteSliceOutcome.RECOVERY_REQUIRED,
            report.project_id,
            report.run_id,
            report.graph_state,
            report.selected_item_id,
            report.selected_scope_id,
            report.source_revision,
            report.baseline_head,
            report.scope_branch,
            report.commit_sha,
            report.runtime_path,
            report.workspace_path,
            issues=(WriteSliceIssue(assessment.reason_code, assessment.human_readable_reason),),
            changeset_digest=report.changeset_digest,
            changed_paths=report.changed_paths,
        )

    def _rehydration_required(
        self, state, execution: WriteExecution, error: WorkspaceError
    ) -> WriteSliceReport:
        report = self._report(state, execution, ())
        return WriteSliceReport(
            WriteSliceOutcome.RECOVERY_REQUIRED,
            report.project_id,
            report.run_id,
            report.graph_state,
            report.selected_item_id,
            report.selected_scope_id,
            report.source_revision,
            report.baseline_head,
            report.scope_branch,
            report.commit_sha,
            report.runtime_path,
            report.workspace_path,
            issues=(
                WriteSliceIssue(
                    getattr(error, "code", None) or "write_rehydration_mismatch", str(error)
                ),
            ),
            changeset_digest=report.changeset_digest,
            changed_paths=report.changed_paths,
        )

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
