"""Durable sequential execution of a frozen, single-scope work plan."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from agentgraph.agents import AgentProvider, DeclaredWorkAgentProvider
from agentgraph.core import (
    CheckpointOutcome,
    CommitMode,
    GraphEngine,
    PolicySnapshot,
    RepairClassification,
    ReviewVerdict,
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
    ImplementNode,
    MoreWorkNode,
    PreflightNode,
    RepairNode,
    ReviewNode,
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

from .delivery_review import DeliveryReviewNode
from .errors import (
    CheckpointError,
    DeliveryReviewProviderRequiredError,
    RepairPolicyError,
    ReviewProviderRequiredError,
    WorkCapabilityMismatchError,
    WorkItemPolicyError,
    WorkPlanMismatchError,
    WorkspaceError,
    WritePreparationError,
)
from .evidence import read_evidence, write_evidence
from .models import (
    WorkPlan,
    WorkPlanItem,
    WriteInputs,
    WriteRunInputs,
    WriteSliceIssue,
    WriteSliceOutcome,
    WriteSliceReport,
    WriteSliceRequest,
)
from .multi_item import (
    DynamicItemNode,
    MultiItemExecution,
    MultiItemSelectWorkNode,
    build_work_plan,
    item_inputs,
)
from .provider import ChangeProvider
from .publish import CreatePullRequestNode, HumanCheckpointDispatchNode
from .remote import RemoteProvider


class WriteSliceRunner:
    """Execute dependency-ready items sequentially within one immutable scope plan."""

    def __init__(
        self,
        repository_root: Path | str,
        work_source: WorkSource,
        change_provider: ChangeProvider,
        *,
        agent_provider: AgentProvider | None = None,
        review_agent_provider: AgentProvider | None = None,
        delivery_review_agent_provider: AgentProvider | None = None,
        remote_provider: RemoteProvider | None = None,
        publish_remote_name: str = "origin",
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
        max_work_items_per_run: int = 1,
        checkpoint_ttl_seconds: int = 3600,
        clock: Callable[[], datetime] = utc_now,
        checkpoint_nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if validation_timeout_seconds <= 0 or max_steps < 1:
            raise ValueError("write runner bounds must be positive")
        if type(max_repair_cycles) is not int or max_repair_cycles not in {0, 1, 2}:
            raise RepairPolicyError("M009 supports max_repair_cycles values 0, 1, or 2")
        if type(max_work_items_per_run) is not int or not 1 <= max_work_items_per_run <= 20:
            raise WorkItemPolicyError("max_work_items_per_run must be between 1 and 20")
        if type(checkpoint_ttl_seconds) is not int or not 60 <= checkpoint_ttl_seconds <= 86400:
            raise ValueError("checkpoint TTL must be between 60 and 86400 seconds")
        self.repository_root = Path(repository_root)
        self.work_source = work_source
        self.change_provider = change_provider
        self.agent_provider = agent_provider or DeclaredWorkAgentProvider()
        self.review_agent_provider = review_agent_provider
        self.delivery_review_agent_provider = delivery_review_agent_provider
        self.remote_provider = remote_provider
        if (
            not isinstance(publish_remote_name, str)
            or not publish_remote_name
            or publish_remote_name.startswith("-")
            or "\x00" in publish_remote_name
        ):
            raise ValueError("publish remote name is invalid")
        self.publish_remote_name = publish_remote_name
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
        self.max_work_items_per_run = max_work_items_per_run
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
        scope = self.work_source.get_scope(inspection.work_snapshot, selection.scope_id)
        if scope.status is not WorkScopeStatus.PLANNED:
            return self._early(
                WriteSliceOutcome.BLOCKED,
                "active_scope_write_not_supported_in_m006",
                "write execution accepts PLANNED scopes only",
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
        if self.git.local_branch_exists(inspection.repository, scope.branch_hint):
            return self._early(
                WriteSliceOutcome.BLOCKED,
                "scope_branch_already_exists",
                "scope branch already exists locally",
                inspection=inspection,
                selection=selection,
            )
        try:
            plan = build_work_plan(self.work_source, inspection.work_snapshot, scope.scope_id)
            selected_plan_item = _plan_item(plan, selection.item_id)
            if selected_plan_item.package != package:
                raise WorkPlanMismatchError("initial selection differs from frozen work plan")
        except Exception as exc:
            code = (
                "work_package_capability_mismatch"
                if isinstance(exc, WorkCapabilityMismatchError)
                else getattr(exc, "code", None) or "work_plan_mismatch"
            )
            return self._early(
                WriteSliceOutcome.INVALID_SOURCE,
                code,
                str(exc),
                inspection=inspection,
                selection=selection,
            )

        assert inspection.git_snapshot.head_sha is not None
        run_inputs = WriteRunInputs(
            1,
            inspection.project_id,
            scope.scope_id,
            scope.parent_scope_id,
            inspection.work_snapshot.revision.fingerprint,
            inspection.git_snapshot.head_sha,
            scope.base_branch_hint,
            scope.branch_hint,
            plan.digest,
            self.max_work_items_per_run,
            self.max_repair_cycles,
            self.review_agent_provider is not None,
            self.checkpoint_ttl_seconds,
            self.delivery_review_agent_provider is not None,
        )
        initial_inputs = item_inputs(
            run_inputs, plan, selected_plan_item, run_inputs.target_baseline_head
        )
        shadow = ShadowInputs(
            inspection, preflight, selection, package, _input_fingerprint(inspection, request)
        )
        run_id = self.run_id_factory()
        self._last_run_id = run_id
        controller, coordinator = self._build_runtime(inspection, shadow, run_inputs, plan, run_id)
        self._coordinator = coordinator

        def initialize(staging: Path) -> None:
            common = {"project_id": run_inputs.project_id, "run_id": run_id}
            write_evidence(staging / "run-inputs.json", context=common, payload=run_inputs)
            write_evidence(staging / "work-plan.json", context=common, payload=plan)
            write_evidence(
                staging / "write-inputs.json",
                context={
                    **common,
                    "item_id": initial_inputs.package.item_id,
                    "scope_id": initial_inputs.package.scope_id,
                    "pinned_head": initial_inputs.baseline_head,
                    "source_revision": initial_inputs.source_revision,
                },
                payload=initial_inputs,
            )

        coordinator.start_run(run_id, initialize_artifacts=initialize)
        with coordinator.open_session(run_id) as session:
            state = session.store.load()
            return self._continue(session, state, controller, ())

    def resume(self, run_id: str) -> WriteSliceReport:
        """Reconstruct frozen authority and continue only from a safe durable boundary."""

        try:
            controller, coordinator = self._existing_runtime(run_id)
        except DeliveryReviewProviderRequiredError as exc:
            return self._early(
                WriteSliceOutcome.RECOVERY_REQUIRED,
                exc.code,
                str(exc),
            )
        except (
            WritePreparationError,
            WorkspaceError,
            RepositoryRootMismatchError,
            WorkSourceRepositoryMismatchError,
            ValueError,
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
        with coordinator.open_session(run_id, recovery=True) as session:
            assessment = session.assess_recovery()
            if assessment.action in {
                RecoveryAction.REAPPLY_RECORDED_RESULT,
                RecoveryAction.COMPLETE_TRANSITION_MARKER,
            }:
                state_for_recovery = session.store.load()
                if (
                    self.remote_provider is not None
                    and state_for_recovery.graph.current_node in {"CREATE_PR", "FINALIZE"}
                    and (controller.run_path / "publish" / "result.json").exists()
                ):
                    try:
                        controller.publication().verify_result_for_recovery(state_for_recovery)
                    except Exception as exc:
                        return replace(
                            self._report(state_for_recovery, controller, ()),
                            outcome=WriteSliceOutcome.RECOVERY_REQUIRED,
                            issues=(
                                WriteSliceIssue(
                                    getattr(exc, "code", "publish_recovery_mismatch"),
                                    str(exc),
                                ),
                            ),
                        )
                assessment = session.recover()
            state = session.store.load()
            try:
                controller.completed_reports(state)
            except WorkspaceError as exc:
                return self._rehydration_required(state, controller, exc)
            if controller.run_inputs.delivery_review_enabled:
                try:
                    controller.delivery_reviews().rehydrate_if_complete(state)
                except WorkspaceError as exc:
                    return self._rehydration_required(state, controller, exc)
            if state.graph.current_node in {
                "SELECT_WORK",
                "EXPLORE",
                "BUILD_TASK_PACKAGE",
                "ASSESS_RISK",
            }:
                try:
                    controller.verify_run_boundary(state)
                except WorkspaceError as exc:
                    return self._rehydration_required(state, controller, exc)
            if assessment.action is RecoveryAction.BLOCKED:
                with suppress(WorkspaceError):
                    self._rehydrate_current(controller, state)
                return self._recovery_required(state, controller, assessment)
            if assessment.action is RecoveryAction.COMPLETED:
                try:
                    return self._report(state, controller, ())
                except WorkspaceError as exc:
                    return self._rehydration_required(state, controller, exc)
            if assessment.action not in {
                RecoveryAction.CLEAN_RESUME,
                RecoveryAction.RERUN_INTERRUPTED_NODE,
            }:
                return self._recovery_required(state, controller, assessment)
            try:
                self._rehydrate_current(controller, state)
            except WorkspaceError as exc:
                return self._rehydration_required(state, controller, exc)
            if (
                state.graph.current_node == "DELIVERY_REVIEW"
                and not controller.run_inputs.delivery_review_enabled
            ):
                try:
                    controller.verify_run_boundary(state)
                except WorkspaceError as exc:
                    return self._rehydration_required(state, controller, exc)
                return self._report(state, controller, ())
            if state.graph.current_node == "HUMAN_CHECKPOINT":
                if state.graph.pending_resume_node == "CREATE_PR":
                    return self._publish_checkpoint_report(state, controller, (), session=session)
                try:
                    checkpoints = controller.checkpoints(state)
                    request, decision = checkpoints.decision(state)
                except CheckpointError as exc:
                    return self._checkpoint_recovery(state, controller, exc)
                if decision is None and not checkpoints.expired(request):
                    return self._report(state, controller, (), checkpoint=checkpoints.view(request))
            return self._continue(session, state, controller, ())

    def assess_recovery(self, run_id: str | None = None) -> RecoveryAssessment:
        selected = run_id or self._last_run_id
        if selected is None:
            raise WritePreparationError("a durable run ID is required")
        _, coordinator = self._existing_runtime(selected)
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
        if self._delivery_checkpoint_pending(run_id) and self.remote_provider is None:
            raise CheckpointError("remote_provider_required")
        try:
            controller, coordinator = self._existing_runtime(run_id)
        except (
            WritePreparationError,
            WorkspaceError,
            RepositoryRootMismatchError,
            WorkSourceRepositoryMismatchError,
            ValueError,
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
            state = session.store.load()
            if (
                state.graph.current_node == "HUMAN_CHECKPOINT"
                and state.graph.pending_resume_node == "CREATE_PR"
            ):
                publish = controller.publication()
                return publish.checkpoints().submit(
                    state,
                    checkpoint_id=checkpoint_id,
                    nonce=nonce,
                    outcome=outcome,
                    actor=actor,
                )
            self._rehydrate_current(controller, state)
            return controller.checkpoints(state).submit(
                state,
                checkpoint_id=checkpoint_id,
                nonce=nonce,
                outcome=outcome,
                actor=actor,
            )

    def _existing_runtime(self, run_id: str) -> tuple[MultiItemExecution, DurableGraphCoordinator]:
        configured = self.repository_root.expanduser().resolve()
        repository = self.git.discover_repository(configured)
        if repository.root != configured:
            raise RepositoryRootMismatchError("configured target must equal the canonical Git root")
        project = self.registry.find_by_root(repository.root)
        if project is None:
            raise WritePreparationError("target repository is absent from the runtime registry")
        run_path = self.paths.run(project.project_id, run_id)
        run_document = read_evidence(run_path / "run-inputs.json")
        plan_document = read_evidence(run_path / "work-plan.json")
        if any(
            document.get("project_id") != project.project_id or document.get("run_id") != run_id
            for document in (run_document, plan_document)
        ):
            raise WorkPlanMismatchError("run authority identity mismatch")
        run_inputs = decode_value(run_document.get("payload"), WriteRunInputs)
        plan = decode_value(plan_document.get("payload"), WorkPlan)
        if run_inputs.max_work_items_per_run != self.max_work_items_per_run:
            raise WorkItemPolicyError("resume work-item policy differs from persisted inputs")
        if run_inputs.max_repair_cycles != self.max_repair_cycles:
            raise RepairPolicyError("resume repair policy differs from persisted inputs")
        if run_inputs.semantic_review_enabled and self.review_agent_provider is None:
            raise ReviewProviderRequiredError(
                "semantic review is enabled for this run but no review provider was supplied"
            )
        if run_inputs.delivery_review_enabled and self.delivery_review_agent_provider is None:
            raise DeliveryReviewProviderRequiredError(
                "delivery review is enabled but no delivery review provider was supplied"
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
            ShadowRequest(scope_id=run_inputs.scope_id),
        )
        reconstructed = build_work_plan(
            self.work_source, inspection.work_snapshot, run_inputs.scope_id
        )
        if reconstructed != plan or plan.digest != run_inputs.work_plan_digest:
            raise WorkPlanMismatchError("work_plan_mismatch")
        scope = self.work_source.get_scope(inspection.work_snapshot, run_inputs.scope_id)
        expected_run_inputs = replace(
            run_inputs,
            project_id=inspection.project_id,
            parent_scope_id=scope.parent_scope_id,
            source_revision=inspection.work_snapshot.revision.fingerprint,
            base_branch=scope.base_branch_hint or "",
            scope_branch=scope.branch_hint or "",
        )
        if expected_run_inputs != run_inputs:
            raise WorkPlanMismatchError("live source differs from persisted run inputs")
        if selection.item_id is None:
            raise WorkPlanMismatchError("initial plan selection is no longer ready")
        first = _plan_item(plan, selection.item_id)
        initial_inputs = item_inputs(run_inputs, plan, first, run_inputs.target_baseline_head)
        root_document = read_evidence(run_path / "write-inputs.json")
        if decode_value(root_document.get("payload"), WriteInputs) != initial_inputs:
            raise WorkPlanMismatchError("root write inputs differ from frozen plan")
        preflight = assess_preflight(inspection, selection)
        shadow = ShadowInputs(
            inspection,
            preflight,
            selection,
            package,
            _input_fingerprint(inspection, WriteSliceRequest(scope_id=run_inputs.scope_id)),
        )
        return self._build_runtime(inspection, shadow, run_inputs, plan, run_id)

    def _build_runtime(
        self,
        inspection,
        shadow: ShadowInputs,
        run_inputs: WriteRunInputs,
        plan: WorkPlan,
        run_id: str,
    ) -> tuple[MultiItemExecution, DurableGraphCoordinator]:
        run_path = self.paths.run(inspection.project_id, run_id)
        controller = MultiItemExecution(
            run_inputs,
            plan,
            inspection.work_snapshot,
            shadow,
            self.work_source,
            self.change_provider,
            self.agent_provider,
            self.review_agent_provider,
            self.delivery_review_agent_provider,
            self.remote_provider,
            self.publish_remote_name,
            self.git,
            self.processes,
            inspection.repository,
            run_id,
            run_path,
            self.identity,
            self.validation_timeout_seconds,
            self.fault or (lambda stage: None),
            self.clock,
            self.checkpoint_nonce_factory,
        )

        def dynamic(node_id, factory):
            return DynamicItemNode(controller, node_id, factory)

        nodes = {
            "START": StartNode(),
            "DISCOVER_PROJECT": DiscoverProjectNode(shadow, shadow=False),
            "PREFLIGHT": PreflightNode(shadow, shadow=False),
            "SELECT_WORK": MultiItemSelectWorkNode(controller),
            "EXPLORE": dynamic("EXPLORE", lambda ex: ExploreNode(ex.inputs, ex.analysis)),
            "BUILD_TASK_PACKAGE": dynamic(
                "BUILD_TASK_PACKAGE", lambda ex: BuildTaskPackageNode(ex.inputs, ex.analysis)
            ),
            "ASSESS_RISK": dynamic(
                "ASSESS_RISK", lambda ex: AssessRiskNode(ex.inputs, ex.analysis)
            ),
            "HUMAN_CHECKPOINT": HumanCheckpointDispatchNode(controller),
            "IMPLEMENT": dynamic("IMPLEMENT", ImplementNode),
            "VALIDATE": dynamic("VALIDATE", ValidateNode),
            "REVIEW": dynamic("REVIEW", ReviewNode),
            "CLASSIFY_FAILURE": dynamic("CLASSIFY_FAILURE", ClassifyFailureNode),
            "PROGRAMMER_REPAIR": dynamic(
                "PROGRAMMER_REPAIR",
                lambda ex: RepairNode(ex, RepairClassification.PROGRAMMER, "PROGRAMMER_REPAIR"),
            ),
            "DEBUGGER": dynamic(
                "DEBUGGER", lambda ex: RepairNode(ex, RepairClassification.DEBUGGER, "DEBUGGER")
            ),
            "CLOSE_TASK": dynamic("CLOSE_TASK", CloseTaskNode),
            "MORE_WORK": MoreWorkNode(),
            "DELIVERY_REVIEW": DeliveryReviewNode(controller.delivery_reviews())
            if run_inputs.delivery_review_enabled
            else None,
            "CREATE_PR": CreatePullRequestNode(controller.publication())
            if self.remote_provider is not None
            else None,
            "FINALIZE": FinalizeNode(),
        }
        nodes = {key: value for key, value in nodes.items() if value is not None}
        policy = PolicySnapshot(
            max_repair_cycles=run_inputs.max_repair_cycles,
            max_work_items_per_run=run_inputs.max_work_items_per_run,
            commit_mode=CommitMode.PER_WORK_ITEM,
        )
        coordinator = DurableGraphCoordinator(
            self.paths,
            self.registry.get(inspection.project_id),
            GraphEngine(canonical_v1_graph(), policy, nodes),
            run_id_factory=lambda: run_id,
            fault=self.fault,
            side_effect_reconciler=(
                controller.publication() if self.remote_provider is not None else None
            ),
        )
        return controller, coordinator

    def _continue(self, session, state, controller, executed_prefix) -> WriteSliceReport:
        executed = list(executed_prefix)
        bound = self.max_steps * controller.run_inputs.max_work_items_per_run
        for _ in range(bound):
            if state.graph.current_node == "END" or (
                state.graph.current_node == "DELIVERY_REVIEW"
                and not controller.run_inputs.delivery_review_enabled
            ):
                break
            if (
                state.graph.current_node == "DELIVERY_REVIEW"
                and controller.run_inputs.delivery_review_enabled
            ):
                try:
                    controller.completed_reports(state)
                    controller.delivery_reviews().rehydrate_if_complete(state)
                except WorkspaceError as exc:
                    return self._rehydration_required(state, controller, exc)
            executed.append(state.graph.current_node)
            state = session.step()
            if state.graph.current_node == "HUMAN_CHECKPOINT":
                if state.graph.pending_resume_node == "CREATE_PR":
                    return self._publish_checkpoint_report(
                        state, controller, tuple(executed), session=session
                    )
                try:
                    checkpoints = controller.checkpoints(state)
                    checkpoint = checkpoints.view(checkpoints.ensure_request(state))
                except CheckpointError as exc:
                    return self._checkpoint_recovery(state, controller, exc)
                if self.fault is not None:
                    self.fault("CHECKPOINT_REQUEST_PERSISTED")
                return self._report(state, controller, tuple(executed), checkpoint=checkpoint)
        else:
            raise WritePreparationError("write graph exceeded its bounded step count")
        if (
            state.graph.current_node == "DELIVERY_REVIEW"
            and not controller.run_inputs.delivery_review_enabled
        ):
            try:
                controller.verify_run_boundary(state)
            except WorkspaceError as exc:
                return self._rehydration_required(state, controller, exc)
        return self._report(state, controller, tuple(executed))

    @staticmethod
    def _rehydrate_current(controller: MultiItemExecution, state) -> None:
        if state.work.item is not None and state.graph.current_node not in {
            "START",
            "DISCOVER_PROJECT",
            "PREFLIGHT",
            "SELECT_WORK",
            "MORE_WORK",
            "FINALIZE",
            "DELIVERY_REVIEW",
            "END",
        }:
            controller.activate(state, rehydrating=True)

    def _report(
        self,
        state,
        controller: MultiItemExecution,
        executed: tuple[str, ...],
        *,
        checkpoint: object | None = None,
    ) -> WriteSliceReport:
        completion_error = None
        try:
            completed_items = controller.completed_reports(state)
        except WorkspaceError as exc:
            completion_error = exc
            completed_items = controller.verified_completed
        publish_error = None
        publish_report = None
        publication_expected = (
            state.run.status is RunStatus.COMPLETED
            and state.graph.current_node == "END"
            and state.review.verdict is ReviewVerdict.PASS
            and state.review.safe_to_create_pr
        )
        if controller.publish_execution is None and (
            publication_expected or (controller.run_path / "publish" / "plan.json").exists()
        ):
            controller.publication()
        if controller.publish_execution is not None:
            try:
                publish_report = controller.publish_execution.report(
                    state, require_result=publication_expected
                )
            except WorkspaceError as exc:
                publish_error = exc
        commits = tuple(item.commit_sha for item in completed_items)
        execution = controller.current
        outcome = (
            WriteSliceOutcome.CHECKPOINT_REQUIRED
            if checkpoint is not None and state.graph.pending_resume_node == "IMPLEMENT"
            else WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED
            if checkpoint is not None and state.graph.pending_resume_node == "CREATE_PR"
            else WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED
            if state.run.status is RunStatus.RUNNING
            and state.graph.current_node == "HUMAN_CHECKPOINT"
            and state.graph.pending_resume_node == "CREATE_PR"
            else WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
            if state.run.status is RunStatus.RUNNING
            and state.graph.current_node == "DELIVERY_REVIEW"
            else WriteSliceOutcome.LOCAL_COMMIT_CREATED
            if commits and state.run.status is RunStatus.PAUSED
            else WriteSliceOutcome.DRAFT_PR_CREATED
            if state.run.status is RunStatus.COMPLETED and publish_report is not None
            else WriteSliceOutcome.CANCELLED
            if state.run.status is RunStatus.CANCELLED
            else WriteSliceOutcome.BLOCKED
            if state.run.status is RunStatus.BLOCKED
            else WriteSliceOutcome.FAILED
        )
        issues = ()
        if completion_error is not None:
            outcome = WriteSliceOutcome.RECOVERY_REQUIRED
            issues = (
                WriteSliceIssue(
                    getattr(completion_error, "code", None) or "multi_item_evidence_mismatch",
                    str(completion_error),
                ),
            )
        elif publish_error is not None:
            outcome = WriteSliceOutcome.RECOVERY_REQUIRED
            issues = (
                WriteSliceIssue(
                    getattr(publish_error, "code", None) or "publish_evidence_mismatch",
                    str(publish_error),
                ),
            )
        elif outcome not in {
            WriteSliceOutcome.LOCAL_COMMIT_CREATED,
            WriteSliceOutcome.CHECKPOINT_REQUIRED,
            WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED,
            WriteSliceOutcome.DELIVERY_CHECKPOINT_REQUIRED,
            WriteSliceOutcome.PUBLISH_CHECKPOINT_REQUIRED,
            WriteSliceOutcome.DRAFT_PR_CREATED,
        }:
            issues = (
                WriteSliceIssue(
                    (execution.issue_code if execution else None)
                    or (execution.analysis.issue_code if execution else None)
                    or (
                        controller.delivery_review_execution.issue_code
                        if controller.delivery_review_execution is not None
                        else None
                    )
                    or (
                        controller.publish_execution.issue_code
                        if controller.publish_execution is not None
                        else None
                    )
                    or state.failure.code
                    or "write_run_not_completed",
                    f"write run finalized with {state.run.status.value}",
                ),
            )
        completed = tuple(item.item_id for item in completed_items)
        stored_completed = tuple(item.id for item in state.work.completed_items)
        selected = (
            state.work.item.id
            if state.work.item
            else stored_completed[-1]
            if stored_completed
            else None
        )
        manifest = None if execution is None else execution.manifest
        changeset = None if execution is None else execution.changeset
        run = controller.run_inputs
        return WriteSliceReport(
            outcome=outcome,
            project_id=run.project_id,
            run_id=controller.run_id,
            graph_state=state,
            selected_item_id=selected,
            selected_scope_id=run.scope_id,
            source_revision=run.source_revision,
            baseline_head=run.target_baseline_head,
            scope_branch=run.scope_branch,
            commit_sha=(
                commits[-1] if commits else None if execution is None else execution.commit_sha
            ),
            runtime_path=str(controller.run_path),
            workspace_path=(
                str(controller.run_path / "workspace")
                if (controller.run_path / "workspace").exists()
                else None
            ),
            executed_nodes=executed,
            issues=issues,
            changeset_digest=None if changeset is None else changeset.digest,
            changed_paths=() if manifest is None else tuple(item.path for item in manifest.files),
            checkpoint=checkpoint,
            completed_item_ids=completed,
            commit_shas=commits,
            completed_items=completed_items,
            delivery_review=(
                None
                if controller.delivery_review_execution is None
                else controller.delivery_review_execution.report
            ),
            publish=publish_report,
        )

    def _publish_checkpoint_report(self, state, controller, executed, *, session=None):
        if self.remote_provider is None:
            return self._report(state, controller, executed)
        continue_run = False
        try:
            publish = controller.publication()
            _local_plan, _local_request, local_decision = (
                publish.checkpoints().decision_for_consumption(state)
            )
            if local_decision is not None:
                if session is not None:
                    continue_run = True
            else:
                plan = publish.ensure_plan(state)
                request, decision = publish.checkpoints().decision(state, plan)
                if decision is None and not publish.checkpoints().expired(request):
                    return self._report(
                        state,
                        controller,
                        executed,
                        checkpoint=publish.checkpoints().view(request, plan),
                    )
                if session is not None:
                    continue_run = True
        except Exception as exc:
            recovery = isinstance(exc, (WorkspaceError, CheckpointError)) or getattr(
                exc, "code", ""
            ) in {
                "publish_evidence_invalid",
                "publish_evidence_mismatch",
                "publish_plan_mismatch",
                "publish_storage_invalid",
            }
            return replace(
                self._report(state, controller, executed),
                outcome=(
                    WriteSliceOutcome.RECOVERY_REQUIRED
                    if recovery
                    else WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED
                ),
                issues=(
                    WriteSliceIssue(
                        getattr(exc, "code", None) or "publish_preparation_blocked",
                        str(exc),
                    ),
                ),
            )
        if continue_run:
            return self._continue(session, state, controller, executed)
        return self._report(state, controller, executed)

    def _checkpoint_recovery(self, state, controller, error: CheckpointError):
        return replace(
            self._report(state, controller, ()),
            outcome=WriteSliceOutcome.RECOVERY_REQUIRED,
            issues=(WriteSliceIssue(error.code, str(error)),),
            checkpoint=None,
        )

    def _pending_checkpoint_report(self, run_id: str, code: str) -> WriteSliceReport | None:
        try:
            repository = self.git.discover_repository(self.repository_root.expanduser().resolve())
            project = self.registry.find_by_root(repository.root)
            if project is None:
                return None
            run_path = self.paths.run(project.project_id, run_id)
            run_inputs = decode_value(
                read_evidence(run_path / "run-inputs.json").get("payload"), WriteRunInputs
            )
            state = StateStore(run_path / "state.json").load()
            if (
                state.run.status is not RunStatus.RUNNING
                or state.graph.current_node != "HUMAN_CHECKPOINT"
            ):
                return None
            return WriteSliceReport(
                outcome=WriteSliceOutcome.RECOVERY_REQUIRED,
                project_id=run_inputs.project_id,
                run_id=run_id,
                graph_state=state,
                selected_item_id=None if state.work.item is None else state.work.item.id,
                selected_scope_id=run_inputs.scope_id,
                source_revision=run_inputs.source_revision,
                baseline_head=run_inputs.target_baseline_head,
                scope_branch=run_inputs.scope_branch,
                commit_sha=None,
                runtime_path=str(run_path),
                workspace_path=None,
                issues=(WriteSliceIssue(code, "checkpoint binding no longer matches"),),
                completed_item_ids=tuple(item.id for item in state.work.completed_items),
            )
        except Exception:
            return None

    def _delivery_checkpoint_pending(self, run_id: str) -> bool:
        try:
            repository = self.git.discover_repository(self.repository_root.expanduser().resolve())
            project = self.registry.find_by_root(repository.root)
            if project is None:
                return False
            state = StateStore(self.paths.run(project.project_id, run_id) / "state.json").load()
            return (
                state.run.status is RunStatus.RUNNING
                and state.graph.current_node == "HUMAN_CHECKPOINT"
                and state.graph.pending_resume_node == "CREATE_PR"
            )
        except Exception:
            return False

    def _recovery_required(self, state, controller, assessment):
        return replace(
            self._report(state, controller, ()),
            outcome=WriteSliceOutcome.RECOVERY_REQUIRED,
            issues=(WriteSliceIssue(assessment.reason_code, assessment.human_readable_reason),),
        )

    def _rehydration_required(self, state, controller, error):
        return replace(
            self._report(state, controller, ()),
            outcome=WriteSliceOutcome.RECOVERY_REQUIRED,
            issues=(
                WriteSliceIssue(
                    getattr(error, "code", None) or "write_rehydration_mismatch",
                    str(error),
                ),
            ),
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
            outcome=outcome,
            project_id=None if inspection is None else inspection.project_id,
            run_id=None,
            graph_state=None,
            selected_item_id=None if selection is None else selection.item_id,
            selected_scope_id=None if selection is None else selection.scope_id,
            source_revision=(
                None if inspection is None else inspection.work_snapshot.revision.fingerprint
            ),
            baseline_head=None if inspection is None else inspection.git_snapshot.head_sha,
            scope_branch=None,
            commit_sha=None,
            runtime_path=None,
            workspace_path=None,
            issues=(WriteSliceIssue(code, message),),
        )


def _plan_item(plan: WorkPlan, item_id: str) -> WorkPlanItem:
    try:
        return next(item for item in plan.items if item.item_id == item_id)
    except StopIteration as exc:
        raise WorkPlanMismatchError("selected item is absent from work plan") from exc


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
