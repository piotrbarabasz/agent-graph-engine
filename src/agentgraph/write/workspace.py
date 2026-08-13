"""Stateful execution capability owned by one durable M006 run."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agentgraph.agents import AgentAnalysisStatus, SemanticReviewVerdict
from agentgraph.agents.prompts import build_semantic_review_prompt
from agentgraph.core import (
    FailureCategory,
    GraphState,
    RepairClassification,
    ReviewVerdict,
    ValidationVerdict,
)
from agentgraph.infra import (
    CommandReceipt,
    CommandSpec,
    DiffCheckResult,
    GitAdapter,
    GitCommitIdentity,
    GitRepository,
    ProcessRunner,
    ProcessStatus,
)
from agentgraph.runtime.codec import decode_value, encode_value
from agentgraph.work import WorkSource

from .apply import apply_changeset
from .capability import normalize_repo_path, path_is_allowed
from .errors import (
    CommitVerificationError,
    PostCommitRecoveryRequired,
    ProviderMutationError,
    RepairFailureContextError,
    RepairLineageError,
    RepairWorkspaceLineageError,
    SemanticReviewBlockedError,
    SemanticReviewContextError,
    SemanticReviewEvidenceError,
    ValidationExecutionError,
    WorkspaceError,
    WorkspaceManifestError,
    WriteBaselineDriftError,
)
from .evidence import read_evidence, write_evidence
from .models import (
    AppliedChangeSet,
    ChangeIntent,
    ChangeRequest,
    ChangeSet,
    CommitWitness,
    RepairFailureContext,
    RepairValidationDiagnostic,
    RepairValidationDiagnosticKind,
    SemanticReviewContext,
    WorkspaceManifest,
    WorkspaceManifestEntry,
    WriteInputs,
)
from .provider import ChangeProvider, ChangeProviderContext

if TYPE_CHECKING:
    from agentgraph.integration import ShadowInputs
    from agentgraph.write.analysis import AgentExecution


@dataclass(slots=True)
class WriteExecution:
    """The only object holding workspace, process, and Git write capabilities."""

    shadow: ShadowInputs
    inputs: WriteInputs
    source: WorkSource
    provider: ChangeProvider
    git: GitAdapter
    processes: ProcessRunner
    target: GitRepository
    run_id: str
    run_path: Path
    analysis: AgentExecution
    commit_identity: GitCommitIdentity
    validation_timeout_seconds: float = 120.0
    rehydrating: bool = False
    workspace: Path = field(init=False)
    operations: Path = field(init=False)
    workspace_repository: GitRepository | None = field(default=None, init=False)
    changeset: ChangeSet | None = field(default=None, init=False)
    applied: AppliedChangeSet | None = field(default=None, init=False)
    manifest: WorkspaceManifest | None = field(default=None, init=False)
    validation_receipts: tuple[CommandReceipt, ...] = field(default=(), init=False)
    diff_check: DiffCheckResult | None = field(default=None, init=False)
    validation_passed: bool = field(default=False, init=False)
    review_passed: bool = field(default=False, init=False)
    review_findings: tuple[str, ...] = field(default=(), init=False)
    review_failure_code: str | None = field(default=None, init=False)
    semantic_blocked: tuple[str, str] | None = field(default=None, init=False)
    commit_sha: str | None = field(default=None, init=False)
    issue_code: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.workspace = self.run_path / "workspace"
        self.operations = self.run_path / "operations"
        run_root = self.run_path.resolve()
        candidate = self.workspace.resolve()
        try:
            candidate.relative_to(run_root)
        except ValueError as exc:
            raise WorkspaceError("workspace escapes durable run directory") from exc
        target = self.target.root.resolve()
        try:
            candidate.relative_to(target)
        except ValueError:
            pass
        else:
            raise WorkspaceError("workspace must be outside target repository")
        if not self.rehydrating and (self.workspace.exists() or self.workspace.is_symlink()):
            raise WorkspaceError("workspace path already exists")

    def implement(self, node_attempt_id: str) -> AppliedChangeSet:
        self.live_recheck("write_baseline_drift")
        if self.git.local_branch_exists(self.target, self.inputs.scope_branch):
            raise WriteBaselineDriftError("scope_branch_already_exists")
        result = self.git.add_worktree(
            self.target,
            self.workspace,
            self.inputs.scope_branch,
            self.inputs.baseline_head,
        )
        self.workspace_repository = result.repository
        workspace_snapshot = self.git.snapshot(result.repository)
        if (
            workspace_snapshot.head_sha != self.inputs.baseline_head
            or workspace_snapshot.branch != self.inputs.scope_branch
            or workspace_snapshot.dirty
            or workspace_snapshot.conflicted_paths
        ):
            raise WorkspaceError("new worktree does not match pinned baseline")
        request = self.change_request()
        provider_directory = self.run_path / "provider"
        provider_directory.mkdir(exist_ok=True)
        context = ChangeProviderContext(
            repository_root=self.workspace,
            runtime_directory=provider_directory,
            baseline_head=self.inputs.baseline_head,
            run_id=self.run_id,
            node_id="IMPLEMENT",
            node_attempt_id=node_attempt_id,
            provider_invocation_id=node_attempt_id,
            repair_cycle=0,
        )
        provider_error: BaseException | None = None
        proposed = None
        try:
            proposed = self.provider.propose(request, context)
        except BaseException as exc:
            provider_error = exc
        provider_snapshot = self.git.snapshot(result.repository)
        self.live_recheck("write_baseline_drift_after_provider")
        if _repository_semantics(provider_snapshot) != _repository_semantics(workspace_snapshot):
            self.issue_code = "provider_mutated_repository"
            raise ProviderMutationError("provider_mutated_repository") from provider_error
        if provider_error is not None:
            self.issue_code = getattr(
                provider_error,
                "reason_code",
                getattr(provider_error, "code", "change_provider_failed"),
            )
            raise provider_error
        if not isinstance(proposed, ChangeSet):
            raise WorkspaceError("ChangeProvider returned a non-ChangeSet value")
        self.changeset = proposed
        write_evidence(
            self.operations / "implement-proposal.json",
            context=self.evidence_context(),
            payload={"request": request, "changeset": proposed},
        )
        applied = apply_changeset(self.workspace, proposed, self.inputs.expected_allowed_paths)
        self.applied = applied
        snapshot = self.git.snapshot(result.repository)
        write_evidence(
            self.operations / "implement-applied.json",
            context=self.evidence_context(),
            payload={"applied": applied, "workspace_snapshot": _snapshot_evidence(snapshot)},
        )
        self.manifest = self.capture_manifest(0)
        write_evidence(
            self.operations / "workspace-manifest.json",
            context=self.evidence_context(),
            payload={"manifest": self.manifest},
        )
        return applied

    def validate(self, cycle: int) -> bool:
        repository = self._require_workspace()
        path = self._cycle_evidence_path(cycle, "validation.json")
        if path.exists():
            self._restore_validation_evidence(cycle)
            return self.validation_passed
        receipts = []
        passed = True
        for check in (*self.inputs.item_validation_checks, *self.inputs.scope_required_checks):
            result = self.processes.run(
                CommandSpec(
                    argv=check.argv,
                    cwd=repository.root,
                    timeout_seconds=self.validation_timeout_seconds,
                )
            )
            receipts.append(result.receipt)
            if result.receipt.status is not ProcessStatus.SUCCEEDED:
                passed = False
        try:
            diff = self.git.diff_check(repository)
        except Exception as exc:
            raise ValidationExecutionError("built-in Git diff check failed") from exc
        self.validation_receipts = tuple(receipts)
        self.diff_check = diff
        self.validation_passed = passed and diff.ok
        write_evidence(
            path,
            context=self.evidence_context(),
            payload={
                "cycle": cycle,
                "workspace_manifest_digest": self._require_manifest().digest,
                "commands": self.validation_receipts,
                "diff_check": diff,
                "passed": self.validation_passed,
            },
        )
        return self.validation_passed

    def review(
        self, state: GraphState, cycle: int, node_attempt_id: str
    ) -> tuple[bool, tuple[str, ...], str | None]:
        repository = self._require_workspace()
        manifest = self._require_manifest()
        path = self._cycle_evidence_path(cycle, "review.json")
        if path.exists():
            self._restore_review_evidence(cycle, state)
            if self.semantic_blocked is not None:
                raise SemanticReviewBlockedError(*self.semantic_blocked)
            return self.review_passed, self.review_findings, self.review_failure_code
        mechanical_path = self._cycle_evidence_path(cycle, "mechanical-review.json")
        snapshot = self.git.snapshot(repository)
        findings: list[str] = []
        expected = {item.path for item in manifest.files}
        try:
            observed_manifest = self.capture_manifest(cycle)
        except WorkspaceError:
            observed_manifest = None
        actual = (
            set() if observed_manifest is None else {item.path for item in observed_manifest.files}
        )
        if snapshot.branch != self.inputs.scope_branch:
            findings.append("workspace_branch_mismatch")
        if snapshot.head_sha != self.inputs.baseline_head:
            findings.append("workspace_head_changed_before_commit")
        if snapshot.staged_paths:
            findings.append("unexpected_staged_changes")
        if snapshot.conflicted_paths:
            findings.append("workspace_conflicts")
        if actual != expected:
            findings.append("changed_paths_mismatch")
        if observed_manifest != manifest:
            findings.append("workspace_manifest_mismatch")
        if not manifest.files:
            findings.append("no_effective_changes")
        for item in manifest.files:
            candidate = repository.root.joinpath(*item.path.split("/"))
            if not candidate.is_file() or candidate.is_symlink():
                findings.append(f"final_file_missing:{item.path}")
            elif _sha256(candidate.read_bytes()) != item.sha256:
                findings.append(f"final_hash_mismatch:{item.path}")
            elif _file_mode(candidate) != item.mode:
                findings.append(f"unexpected_mode_change:{item.path}")
        if not self.validation_passed or self.diff_check is None or not self.diff_check.ok:
            findings.append("validation_not_passed")
        try:
            self.live_recheck("review_baseline_drift")
        except WriteBaselineDriftError:
            findings.append("review_baseline_drift")
        mechanical_findings = tuple(dict.fromkeys(findings))
        mechanical_passed = not mechanical_findings
        mechanical_payload = {
            "cycle": cycle,
            "workspace_manifest_digest": manifest.digest,
            "passed": mechanical_passed,
            "findings": mechanical_findings,
        }
        if mechanical_path.exists():
            if self._payload_path(mechanical_path) != mechanical_payload:
                raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch")
        else:
            write_evidence(
                mechanical_path,
                context=self.evidence_context(repair_cycle=cycle),
                payload=mechanical_payload,
            )

        semantic_payload = None
        final_findings = mechanical_findings
        failure_code = None if mechanical_passed else "deterministic_review_failed"
        if mechanical_passed and self.inputs.semantic_review_enabled:
            review_context = self.prepare_semantic_review_context(state, cycle)
            result = self.analysis.semantic_review(
                node_attempt_id,
                build_semantic_review_prompt(review_context),
                repository_root=self.workspace,
                expected_workspace_digest=manifest.digest,
                workspace_digest=self.verify_current_manifest,
            )
            value = result.value
            semantic_payload = {
                "status": value.status.value,
                "verdict": None if value.verdict is None else value.verdict.value,
                "summary": value.summary,
                "findings": encode_value(value.findings),
                "reason_code": value.reason_code,
                "message": value.message,
                "input_digest": result.response.input_digest,
                "output_digest": result.response.output_digest,
                "evidence_reference": result.evidence_reference,
                "context_digest": review_context.digest,
            }
            if value.status is AgentAnalysisStatus.BLOCKED:
                assert value.reason_code is not None and value.message is not None
                self.semantic_blocked = (value.reason_code, value.message)
            elif value.verdict is SemanticReviewVerdict.FAIL:
                final_findings = tuple(_project_semantic_finding(item) for item in value.findings)
                failure_code = "semantic_review_failed"

        self.review_findings = final_findings
        self.review_passed = mechanical_passed and (
            not self.inputs.semantic_review_enabled
            or (
                semantic_payload is not None
                and semantic_payload["status"] == "success"
                and semantic_payload["verdict"] == "pass"
            )
        )
        self.review_failure_code = failure_code
        write_evidence(
            path,
            context=self.evidence_context(repair_cycle=cycle),
            payload={
                "cycle": cycle,
                "workspace_manifest_digest": manifest.digest,
                "mechanical": {
                    "passed": mechanical_passed,
                    "findings": mechanical_findings,
                },
                "semantic_review_enabled": self.inputs.semantic_review_enabled,
                "semantic": semantic_payload,
                "passed": self.review_passed,
                "findings": self.review_findings,
                "failure_code": self.review_failure_code,
            },
        )
        if self.semantic_blocked is not None:
            raise SemanticReviewBlockedError(*self.semantic_blocked)
        return self.review_passed, self.review_findings, self.review_failure_code

    def prepare_semantic_review_context(
        self, state: GraphState, cycle: int
    ) -> SemanticReviewContext:
        manifest = self._require_manifest()
        if (
            state.validation.verdict is not ValidationVerdict.PASS
            or state.repair.count != cycle
            or manifest.cycle != cycle
            or state.changes.agent_reported_files != tuple(item.path for item in manifest.files)
        ):
            raise SemanticReviewContextError("semantic_review_context_mismatch")
        self.live_recheck("review_baseline_drift")
        self.verify_current_manifest()
        self._restore_validation_evidence(cycle)
        if not self.validation_passed or self.diff_check is None or not self.diff_check.ok:
            raise SemanticReviewContextError("semantic_review_context_mismatch")
        if state.validation.checks != tuple(
            receipt.command_id for receipt in self.validation_receipts
        ):
            raise SemanticReviewContextError("semantic_review_context_mismatch")
        diagnostics = self._current_validation_diagnostics()
        if any(item.status != ProcessStatus.SUCCEEDED.value for item in diagnostics):
            raise SemanticReviewContextError("semantic_review_context_mismatch")
        requirements, acceptance = self.analysis.implementation_requirements()
        if (
            requirements != state.requirements.items
            or acceptance != state.acceptance_criteria.items
        ):
            raise SemanticReviewContextError("semantic_review_context_mismatch")
        explore = self.analysis.explore_analysis
        context = SemanticReviewContext.create(
            cycle=cycle,
            item_id=self.inputs.package.item_id,
            scope_id=self.inputs.package.scope_id,
            goal=self.inputs.package.goal,
            current_manifest_digest=manifest.digest,
            current_changed_paths=tuple(item.path for item in manifest.files),
            effective_requirements=requirements,
            effective_acceptance_criteria=acceptance,
            architecture_invariants=state.architecture_invariants.items,
            derived_constraints=() if explore is None else explore.derived_constraints,
            validation_diagnostics=diagnostics,
            allowed_paths=self.inputs.expected_allowed_paths,
            baseline_head=self.inputs.baseline_head,
            source_revision=self.inputs.source_revision,
            risk_level=state.risk.level,
            relevant_files=() if explore is None else explore.relevant_files,
        )
        context_path = self._cycle_evidence_path(cycle, "review-context.json")
        if context_path.exists():
            try:
                restored = decode_value(
                    self._payload_path(context_path)["context"], SemanticReviewContext
                )
            except Exception as exc:
                raise SemanticReviewContextError("semantic_review_context_mismatch") from exc
            if restored != context:
                raise SemanticReviewContextError("semantic_review_context_mismatch")
        else:
            write_evidence(
                context_path,
                context=self.evidence_context(repair_cycle=cycle),
                payload={"context": context, "context_digest": context.digest},
            )
        return context

    def capture_manifest(self, cycle: int) -> WorkspaceManifest:
        """Capture the exact current effective diff against the pinned baseline."""

        repository = self._require_workspace()
        snapshot = self.git.snapshot(repository)
        if (
            snapshot.head_sha != self.inputs.baseline_head
            or snapshot.branch != self.inputs.scope_branch
            or snapshot.detached_head
            or snapshot.staged_paths
            or snapshot.conflicted_paths
        ):
            raise WorkspaceManifestError("workspace_manifest_invalid")
        raw_paths = (*snapshot.unstaged_paths, *snapshot.untracked_paths)
        names = tuple(sorted({path.as_posix() for path in raw_paths}, key=lambda x: x.encode()))
        entries: list[WorkspaceManifestEntry] = []
        root = repository.root.resolve(strict=True)
        for name in names:
            pure = normalize_repo_path(name)
            if not path_is_allowed(name, self.inputs.expected_allowed_paths):
                raise WorkspaceManifestError("workspace_manifest_invalid")
            candidate = root.joinpath(*pure.parts)
            try:
                candidate.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise WorkspaceManifestError("workspace_manifest_invalid") from exc
            if not candidate.is_file() or candidate.is_symlink():
                raise WorkspaceManifestError("workspace_manifest_invalid")
            raw = candidate.read_bytes()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkspaceManifestError("workspace_manifest_invalid") from exc
            baseline = self.git.tree_entry(repository, self.inputs.baseline_head, name)
            if baseline is not None:
                if baseline.object_type != "blob" or baseline.mode not in {"100644", "100755"}:
                    raise WorkspaceManifestError("workspace_manifest_invalid")
                mode = 0o755 if baseline.mode == "100755" else 0o644
                if _file_mode(candidate) != mode:
                    raise WorkspaceManifestError("workspace_manifest_invalid")
                if raw == self.git.read_blob(repository, baseline.object_id):
                    # Git status can briefly report a path whose bytes have returned to baseline.
                    continue
            else:
                mode = 0o644
                if _file_mode(candidate) != mode:
                    raise WorkspaceManifestError("workspace_manifest_invalid")
            entries.append(WorkspaceManifestEntry(name, _sha256(raw), len(raw), mode))
        return WorkspaceManifest.create(cycle, self.inputs.baseline_head, tuple(entries))

    def verify_current_manifest(self) -> str:
        manifest = self._require_manifest()
        try:
            observed = self.capture_manifest(manifest.cycle)
        except WorkspaceError as exc:
            raise RepairWorkspaceLineageError("repair_workspace_lineage_mismatch") from exc
        if observed != manifest:
            raise RepairWorkspaceLineageError("repair_workspace_lineage_mismatch")
        return observed.digest

    def prepare_failure_context(self, state: GraphState) -> RepairFailureContext:
        if len(state.repair.history) != state.repair.count:
            raise RepairFailureContextError("repair_failure_context_mismatch")
        source_node = state.graph.previous_node
        if source_node not in {"VALIDATE", "REVIEW"}:
            raise RepairFailureContextError("repair_failure_context_mismatch")
        self.live_recheck("write_baseline_drift_before_failure_classification")
        manifest = self._require_manifest()
        self.verify_current_manifest()
        cycle = state.repair.count
        if source_node == "VALIDATE":
            if (
                state.validation.verdict is not ValidationVerdict.FAIL
                or state.failure.category
                not in {
                    FailureCategory.VALIDATION,
                    FailureCategory.IMPLEMENTATION,
                    FailureCategory.DESIGN,
                }
            ):
                raise RepairFailureContextError("repair_failure_context_mismatch")
            self._restore_validation_evidence(cycle)
            if self.validation_passed:
                raise RepairFailureContextError("repair_failure_context_mismatch")
            receipts = self.validation_receipts
            review_findings: tuple[str, ...] = ()
        else:
            if (
                state.validation.verdict is not ValidationVerdict.PASS
                or state.review.verdict is not ReviewVerdict.FAIL
                or (state.review.findings != self.review_findings and self.review_findings)
            ):
                raise RepairFailureContextError("repair_failure_context_mismatch")
            self._restore_validation_evidence(cycle)
            self._restore_review_evidence(cycle, state)
            if (
                not self.validation_passed
                or self.review_passed
                or state.review.findings != self.review_findings
            ):
                raise RepairFailureContextError("repair_failure_context_mismatch")
            receipts = self.validation_receipts
            review_findings = self.review_findings
        diff_check = self.diff_check
        if diff_check is None:
            raise RepairFailureContextError("repair_failure_context_mismatch")
        diagnostics = (
            *(
                self._validation_diagnostic(
                    RepairValidationDiagnosticKind.DECLARED_VALIDATION, item
                )
                for item in receipts
            ),
            self._validation_diagnostic(
                RepairValidationDiagnosticKind.GIT_DIFF_CHECK_WORKTREE,
                diff_check.receipts[0],
            ),
            self._validation_diagnostic(
                RepairValidationDiagnosticKind.GIT_DIFF_CHECK_STAGED,
                diff_check.receipts[1],
            ),
        )
        requirements, acceptance = self.analysis.implementation_requirements()
        context = RepairFailureContext(
            cycle + 1,
            source_node,
            state.failure.category or FailureCategory.VALIDATION,
            state.failure.code or "repairable_failure",
            manifest.digest,
            tuple(item.path for item in manifest.files),
            diagnostics,
            review_findings,
            requirements,
            acceptance,
            self.inputs.expected_allowed_paths,
            self.inputs.baseline_head,
            self.inputs.source_revision,
        )
        path = self._cycle_evidence_path(cycle + 1, "context.json")
        if path.exists():
            try:
                restored = decode_value(self._payload_path(path), RepairFailureContext)
            except Exception as exc:
                raise RepairFailureContextError("repair_failure_context_mismatch") from exc
            if restored != context:
                raise RepairFailureContextError("repair_failure_context_mismatch")
        else:
            write_evidence(path, context=self.evidence_context(), payload=context)
        return context

    def repair(
        self,
        state: GraphState,
        node_id: str,
        node_attempt_id: str,
        classification: RepairClassification,
    ) -> tuple[AppliedChangeSet, WorkspaceManifest]:
        cycle = state.repair.count
        if cycle < 1 or len(state.repair.history) != cycle:
            raise RepairFailureContextError("repair_failure_context_mismatch")
        expected_record = state.repair.history[-1]
        if (
            expected_record.id != f"repair-{cycle:03d}"
            or expected_record.classification is not classification
        ):
            raise RepairFailureContextError("repair_failure_context_mismatch")
        context_path = self._cycle_evidence_path(cycle, "context.json")
        try:
            failure_context = decode_value(self._payload_path(context_path), RepairFailureContext)
        except Exception as exc:
            raise RepairFailureContextError("repair_failure_context_mismatch") from exc
        if failure_context.cycle != cycle or failure_context.classification not in {
            None,
            classification,
        }:
            raise RepairFailureContextError("repair_failure_context_mismatch")
        self.live_recheck("write_baseline_drift_before_repair_provider")
        before = self._require_manifest()
        self.verify_current_manifest()
        request = self.change_request(
            intent=(
                ChangeIntent.PROGRAMMER_REPAIR
                if classification is RepairClassification.PROGRAMMER
                else ChangeIntent.DEBUGGER
            ),
            failure_context=failure_context,
        )
        provider_directory = self.run_path / "provider" / "repairs" / f"{cycle:03d}"
        provider_directory.mkdir(parents=True, exist_ok=False)
        provider_invocation_id = f"{node_attempt_id}-repair-{cycle:03d}"
        provider_context = ChangeProviderContext(
            self.workspace,
            provider_directory,
            self.inputs.baseline_head,
            self.run_id,
            node_id,
            node_attempt_id,
            provider_invocation_id,
            cycle,
        )
        provider_error: BaseException | None = None
        proposed = None
        try:
            proposed = self.provider.propose(request, provider_context)
        except BaseException as exc:
            provider_error = exc
        try:
            after_provider = self.capture_manifest(before.cycle)
        except WorkspaceError as exc:
            self.issue_code = "provider_mutated_repository"
            raise ProviderMutationError("provider_mutated_repository") from provider_error or exc
        self.live_recheck("write_baseline_drift_after_repair_provider")
        if after_provider != before:
            self.issue_code = "provider_mutated_repository"
            raise ProviderMutationError("provider_mutated_repository") from provider_error
        if provider_error is not None:
            raise provider_error
        if not isinstance(proposed, ChangeSet):
            raise WorkspaceError("ChangeProvider returned a non-ChangeSet value")
        repair_dir = self.operations / "repairs" / f"{cycle:03d}"
        write_evidence(
            repair_dir / "proposal.json",
            context=self.evidence_context(changeset_digest=proposed.digest, repair_cycle=cycle),
            payload={"request": request, "changeset": proposed},
        )
        applied = apply_changeset(self.workspace, proposed, self.inputs.expected_allowed_paths)
        manifest = self.capture_manifest(cycle)
        write_evidence(
            repair_dir / "applied.json",
            context=self.evidence_context(changeset_digest=proposed.digest, repair_cycle=cycle),
            payload={
                "applied": applied,
                "workspace_snapshot": _snapshot_evidence(
                    self.git.snapshot(self._require_workspace())
                ),
            },
        )
        write_evidence(
            repair_dir / "workspace-manifest.json",
            context=self.evidence_context(changeset_digest=proposed.digest, repair_cycle=cycle),
            payload={"manifest": manifest},
        )
        self.changeset = proposed
        self.applied = applied
        self.manifest = manifest
        self.validation_passed = False
        self.review_passed = False
        self.validation_receipts = ()
        self.review_findings = ()
        return applied, manifest

    def commit(self) -> str:
        if not self.review_passed:
            raise CommitVerificationError("review has not authorized close")
        self.live_recheck("write_baseline_drift_before_commit")
        repository = self._require_workspace()
        manifest = self._require_manifest()
        if not manifest.files:
            raise CommitVerificationError("no effective changes to commit")
        self.verify_current_manifest()
        before = self.git.snapshot(repository)
        if before.staged_paths or before.conflicted_paths:
            raise CommitVerificationError("workspace changed after review")
        expected = tuple(item.path for item in manifest.files)
        if {item.path for item in self.capture_manifest(manifest.cycle).files} != set(expected):
            raise CommitVerificationError("workspace paths changed after review")
        for item in manifest.files:
            candidate = repository.root.joinpath(*item.path.split("/"))
            if (
                not candidate.is_file()
                or candidate.is_symlink()
                or _sha256(candidate.read_bytes()) != item.sha256
                or _file_mode(candidate) != item.mode
            ):
                raise CommitVerificationError("reviewed file content or mode changed before commit")
        final_diff_check = self.git.diff_check(repository)
        if not final_diff_check.ok:
            raise CommitVerificationError("workspace diff check changed after review")
        scope_ref = f"refs/heads/{self.inputs.scope_branch}"
        previous_branch_head = self.git.resolve_ref(self.target, scope_ref)
        if previous_branch_head != before.head_sha:
            raise CommitVerificationError("scope branch changed before commit")
        stage = self.git.stage_paths(repository, expected)
        try:
            result = self.git.commit(
                repository,
                f"agentgraph({self.inputs.package.item_id}): {self.inputs.package.title}",
                expected_paths=expected,
                identity=self.commit_identity,
            )
        except Exception as exc:
            resolution_failed = False
            try:
                observed = self.git.resolve_ref(self.target, scope_ref)
            except Exception:
                observed = None
                resolution_failed = True
            if observed is not None and observed != previous_branch_head:
                self.commit_sha = observed
                try:
                    self._write_commit_witness(previous_branch_head, observed, expected)
                except Exception as witness_exc:
                    raise PostCommitRecoveryRequired(
                        "commit advanced but witness finalization failed"
                    ) from witness_exc
                raise PostCommitRecoveryRequired(
                    "commit side effect occurred before GitAdapter returned"
                ) from exc
            if resolution_failed:
                try:
                    self._write_commit_witness(previous_branch_head, None, expected)
                except Exception as witness_exc:
                    raise PostCommitRecoveryRequired(
                        "commit outcome and witness finalization are uncertain"
                    ) from witness_exc
                raise PostCommitRecoveryRequired("commit outcome could not be resolved") from exc
            raise

        self.commit_sha = result.commit_sha
        try:
            self._write_commit_witness(previous_branch_head, result.commit_sha, expected)
        except Exception as exc:
            raise PostCommitRecoveryRequired("commit witness finalization failed") from exc
        try:
            self._verify_committed_tree(repository, result.commit_sha, manifest)
            final = self.git.snapshot(repository)
            if not self._snapshot_matches_head(repository, final, result.commit_sha):
                raise CommitVerificationError("committed workspace verification failed")
            if self.git.resolve_ref(self.target, scope_ref) != result.commit_sha:
                raise CommitVerificationError("scope branch ref does not equal commit")
            self.live_recheck("write_baseline_drift_after_commit")
            write_evidence(
                self.operations / "commit.json",
                context=self.evidence_context(),
                payload={
                    "commit_sha": result.commit_sha,
                    "stage_receipt": stage,
                    "commit_receipt": result.receipt,
                },
            )
        except Exception as exc:
            raise PostCommitRecoveryRequired("post-commit verification requires recovery") from exc
        return result.commit_sha

    def _snapshot_matches_head(
        self, repository: GitRepository, snapshot: object, commit_sha: str
    ) -> bool:
        if (
            snapshot.branch != self.inputs.scope_branch
            or snapshot.head_sha != commit_sha
            or snapshot.staged_paths
            or snapshot.conflicted_paths
        ):
            return False
        paths = {
            path.as_posix()
            for group in (snapshot.unstaged_paths, snapshot.untracked_paths)
            for path in group
        }
        for name in paths:
            candidate = repository.root.joinpath(*normalize_repo_path(name).parts)
            entry = self.git.tree_entry(repository, commit_sha, name)
            if (
                entry is None
                or entry.object_type != "blob"
                or not candidate.is_file()
                or candidate.is_symlink()
                or candidate.read_bytes() != self.git.read_blob(repository, entry.object_id)
                or _file_mode(candidate) != (0o755 if entry.mode == "100755" else 0o644)
            ):
                return False
        return True

    def _verify_committed_tree(
        self, repository: GitRepository, commit_sha: str, manifest: WorkspaceManifest
    ) -> None:
        parents = self.git.commit_parents(repository, commit_sha)
        if parents != (self.inputs.baseline_head,):
            raise CommitVerificationError("commit parent differs from pinned baseline")
        expected_paths = tuple(item.path for item in manifest.files)
        committed_paths = tuple(
            path.as_posix()
            for path in self.git.diff_paths_between(
                repository, self.inputs.baseline_head, commit_sha
            )
        )
        if set(committed_paths) != set(expected_paths) or len(committed_paths) != len(
            expected_paths
        ):
            raise CommitVerificationError("committed paths differ from reviewed paths")
        for item in manifest.files:
            entry = self.git.tree_entry(repository, commit_sha, item.path)
            expected_mode = "100755" if item.mode & 0o111 else "100644"
            if entry is None or entry.object_type != "blob" or entry.mode != expected_mode:
                raise CommitVerificationError("committed entry type or mode is unsupported")
            if _sha256(self.git.read_blob(repository, entry.object_id)) != item.sha256:
                raise CommitVerificationError("committed blob differs from reviewed content")

    def rehydrate(self, state: GraphState) -> None:
        """Restore execution facts from immutable evidence for the persisted cursor."""

        cursor = state.graph.current_node
        if self.workspace.exists():
            if self.workspace.is_symlink():
                raise WorkspaceError("persisted workspace is a symlink")
            repository = self.git.discover_repository(self.workspace)
            if repository.root != self.workspace.resolve():
                raise WorkspaceError("persisted workspace root differs from expected path")
            snapshot = self.git.snapshot(repository)
            if snapshot.branch != self.inputs.scope_branch:
                raise WorkspaceError("persisted workspace branch differs from write inputs")
            self.workspace_repository = repository

        after_implement = cursor in {
            "VALIDATE",
            "REVIEW",
            "CLASSIFY_FAILURE",
            "PROGRAMMER_REPAIR",
            "DEBUGGER",
            "CLOSE_TASK",
            "MORE_WORK",
            "FINALIZE",
        } or bool(state.changes.identifiers)
        if after_implement:
            if len(state.repair.history) != state.repair.count:
                raise RepairLineageError("repair_lineage_mismatch")
            self.analysis.prepare_implementation(state)
            proposal = self._payload("implement-proposal.json")
            applied_payload = self._payload("implement-applied.json")
            self.changeset = decode_value(proposal["changeset"], ChangeSet)
            self.applied = decode_value(applied_payload["applied"], AppliedChangeSet)
            if self.changeset.digest != self.applied.changeset_digest:
                raise WorkspaceError("proposal and applied evidence disagree")
            self.manifest = decode_value(
                self._payload("workspace-manifest.json")["manifest"], WorkspaceManifest
            )
            if self.manifest.cycle != 0:
                raise RepairLineageError("repair_lineage_mismatch")
            identifiers = state.changes.identifiers
            if (
                f"changeset:{self.changeset.digest}" not in identifiers
                or f"workspace_manifest:0:{self.manifest.digest}" not in identifiers
            ):
                raise RepairLineageError("repair_lineage_mismatch")
            successful_cycles: list[int] = []
            for value in identifiers:
                if value.startswith("workspace_manifest:"):
                    parts = value.split(":", 2)
                    if len(parts) != 3 or not parts[1].isdigit():
                        raise RepairLineageError("repair_lineage_mismatch")
                    successful_cycles.append(int(parts[1]))
            if sorted(successful_cycles) != list(range(max(successful_cycles, default=-1) + 1)):
                raise RepairLineageError("repair_lineage_mismatch")
            if successful_cycles and max(successful_cycles) > state.repair.count:
                raise RepairLineageError("repair_lineage_mismatch")
            for cycle in range(1, max(successful_cycles, default=0) + 1):
                repair_dir = self.operations / "repairs" / f"{cycle:03d}"
                try:
                    record = state.repair.history[cycle - 1]
                    if record.id != f"repair-{cycle:03d}":
                        raise RepairLineageError("repair_lineage_mismatch")
                    proposal_document = self._evidence_document_path(repair_dir / "proposal.json")
                    applied_document = self._evidence_document_path(repair_dir / "applied.json")
                    manifest_document = self._evidence_document_path(
                        repair_dir / "workspace-manifest.json"
                    )
                    proposal_payload = self._evidence_payload(proposal_document)
                    applied_payload = self._evidence_payload(applied_document)
                    manifest_payload = self._evidence_payload(manifest_document)
                    changeset = decode_value(proposal_payload["changeset"], ChangeSet)
                    applied = decode_value(applied_payload["applied"], AppliedChangeSet)
                    manifest = decode_value(manifest_payload["manifest"], WorkspaceManifest)
                except Exception as exc:
                    raise RepairLineageError("repair_lineage_mismatch") from exc
                if (
                    applied.changeset_digest != changeset.digest
                    or manifest.cycle != cycle
                    or any(
                        document.get("changeset_digest") != changeset.digest
                        or document.get("repair_cycle") != cycle
                        for document in (
                            proposal_document,
                            applied_document,
                            manifest_document,
                        )
                    )
                    or decode_value(proposal_payload["request"], ChangeRequest).intent
                    is not (
                        ChangeIntent.PROGRAMMER_REPAIR
                        if record.classification is RepairClassification.PROGRAMMER
                        else ChangeIntent.DEBUGGER
                    )
                    or f"repair_changeset:{cycle}:{changeset.digest}" not in identifiers
                    or f"workspace_manifest:{cycle}:{manifest.digest}" not in identifiers
                ):
                    raise RepairLineageError("repair_lineage_mismatch")
                self.changeset, self.applied, self.manifest = changeset, applied, manifest
            manifest = self._require_manifest()
            manifest_paths = tuple(item.path for item in manifest.files)
            if state.changes.agent_reported_files != manifest_paths or state.changes.count != len(
                manifest_paths
            ):
                raise RepairLineageError("repair_lineage_mismatch")
            witness_exists = (self.operations / "commit-witness.json").exists()
            if cursor not in {"MORE_WORK", "FINALIZE", "END"} and not witness_exists:
                self.verify_current_manifest()

        current_cycle = 0 if self.manifest is None else self.manifest.cycle
        validation_path = self._cycle_evidence_path(current_cycle, "validation.json", create=False)
        after_validation = (
            validation_path.exists()
            and state.validation.verdict is not ValidationVerdict.UNKNOWN
            and cursor not in {"PROGRAMMER_REPAIR", "DEBUGGER", "VALIDATE"}
        )
        if after_validation:
            self._restore_validation_evidence(current_cycle)
            expected = ValidationVerdict.PASS if self.validation_passed else ValidationVerdict.FAIL
            receipt_ids = tuple(receipt.command_id for receipt in self.validation_receipts)
            if state.validation.verdict is not expected or state.validation.checks != receipt_ids:
                raise WorkspaceError("GraphState and validation evidence disagree")

        review_path = self._cycle_evidence_path(current_cycle, "review.json", create=False)
        after_review = (
            review_path.exists()
            and state.review.verdict is not ReviewVerdict.UNKNOWN
            and cursor not in {"PROGRAMMER_REPAIR", "DEBUGGER", "VALIDATE", "REVIEW"}
        )
        if after_review:
            self._restore_review_evidence(current_cycle, state)
            expected = ReviewVerdict.PASS if self.review_passed else ReviewVerdict.FAIL
            if (
                state.review.verdict is not expected
                or state.review.safe_to_close is not self.review_passed
                or state.review.findings != self.review_findings
            ):
                raise WorkspaceError("GraphState and review evidence disagree")

        witness_path = self.operations / "commit-witness.json"
        if witness_path.exists():
            witness = decode_value(self._payload("commit-witness.json"), CommitWitness)
            if (
                witness.project_id != self.inputs.project_id
                or witness.run_id != self.run_id
                or witness.item_id != self.inputs.package.item_id
                or witness.scope_id != self.inputs.package.scope_id
                or witness.base_head != self.inputs.baseline_head
                or witness.previous_branch_head != self.inputs.baseline_head
                or self.changeset is None
                or witness.changeset_digest != self.changeset.digest
                or self.manifest is None
                or witness.workspace_manifest_digest != self.manifest.digest
                or witness.repair_count != self.manifest.cycle
                or witness.reviewed_paths != tuple(item.path for item in self.manifest.files)
            ):
                raise WorkspaceError("commit witness differs from persisted write inputs")
            self.commit_sha = witness.commit_sha

    def _write_commit_witness(
        self, previous_branch_head: str, commit_sha: str | None, reviewed_paths: tuple[str, ...]
    ) -> None:
        changeset = self.changeset
        manifest = self.manifest
        if changeset is None or manifest is None:
            raise WorkspaceError("commit witness requires a changeset")
        witness = CommitWitness(
            self.inputs.project_id,
            self.run_id,
            self.inputs.package.item_id,
            self.inputs.package.scope_id,
            self.inputs.baseline_head,
            previous_branch_head,
            commit_sha,
            changeset.digest,
            reviewed_paths,
            manifest.digest,
            manifest.cycle,
        )
        write_evidence(
            self.operations / "commit-witness.json",
            context=self.evidence_context(),
            payload=witness,
        )

    def _restore_validation_evidence(self, cycle: int) -> None:
        validation = self._payload_path(
            self._cycle_evidence_path(cycle, "validation.json", create=False)
        )
        manifest = self._require_manifest()
        if (
            validation.get("cycle") != cycle
            or validation.get("workspace_manifest_digest") != manifest.digest
        ):
            raise RepairFailureContextError("repair_failure_context_mismatch")
        self.validation_receipts = decode_value(validation["commands"], tuple[CommandReceipt, ...])
        self.diff_check = decode_value(validation["diff_check"], DiffCheckResult)
        self.validation_passed = bool(validation["passed"])

    def _restore_review_evidence(self, cycle: int, state: GraphState) -> None:
        review = self._payload_path(self._cycle_evidence_path(cycle, "review.json", create=False))
        manifest = self._require_manifest()
        if (
            set(review)
            != {
                "cycle",
                "workspace_manifest_digest",
                "mechanical",
                "semantic_review_enabled",
                "semantic",
                "passed",
                "findings",
                "failure_code",
            }
            or review.get("cycle") != cycle
            or review.get("workspace_manifest_digest") != manifest.digest
            or review.get("semantic_review_enabled") is not self.inputs.semantic_review_enabled
        ):
            raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch")
        mechanical = review.get("mechanical")
        mechanical_path = self._cycle_evidence_path(cycle, "mechanical-review.json", create=False)
        try:
            mechanical_evidence = self._payload_path(mechanical_path)
        except Exception as exc:
            raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch") from exc
        if (
            not isinstance(mechanical, dict)
            or mechanical
            != {
                "passed": mechanical_evidence.get("passed"),
                "findings": mechanical_evidence.get("findings"),
            }
            or mechanical_evidence.get("cycle") != cycle
            or mechanical_evidence.get("workspace_manifest_digest") != manifest.digest
        ):
            raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch")
        semantic = review.get("semantic")
        self.semantic_blocked = None
        if self.inputs.semantic_review_enabled and mechanical.get("passed") is True:
            if not isinstance(semantic, dict) or set(semantic) != {
                "status",
                "verdict",
                "summary",
                "findings",
                "reason_code",
                "message",
                "input_digest",
                "output_digest",
                "evidence_reference",
                "context_digest",
            }:
                raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch")
            context = self.prepare_semantic_review_context(state, cycle)
            if semantic.get("context_digest") != context.digest:
                raise SemanticReviewContextError("semantic_review_context_mismatch")
            try:
                value = self.analysis.restore_semantic_review(
                    semantic.get("evidence_reference"),
                    semantic.get("output_digest"),
                    semantic.get("input_digest"),
                )
            except Exception as exc:
                raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch") from exc
            if (
                semantic.get("status") != value.status.value
                or semantic.get("verdict")
                != (None if value.verdict is None else value.verdict.value)
                or semantic.get("summary") != value.summary
                or semantic.get("findings") != encode_value(value.findings)
                or semantic.get("reason_code") != value.reason_code
                or semantic.get("message") != value.message
            ):
                raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch")
            if value.status is AgentAnalysisStatus.BLOCKED:
                assert value.reason_code is not None and value.message is not None
                self.semantic_blocked = (value.reason_code, value.message)
            projected = tuple(_project_semantic_finding(item) for item in value.findings)
            expected_pass = value.verdict is SemanticReviewVerdict.PASS
            expected_code = (
                "semantic_review_failed" if value.verdict is SemanticReviewVerdict.FAIL else None
            )
            expected_findings = projected
        elif semantic is not None:
            raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch")
        else:
            expected_pass = bool(mechanical.get("passed"))
            expected_code = None if expected_pass else "deterministic_review_failed"
            expected_findings = tuple(mechanical.get("findings", ()))
        if (
            review.get("passed") is not expected_pass
            or tuple(review.get("findings", ())) != expected_findings
            or review.get("failure_code") != expected_code
        ):
            raise SemanticReviewEvidenceError("semantic_review_evidence_mismatch")
        self.review_passed = bool(review["passed"])
        self.review_findings = tuple(review["findings"])
        self.review_failure_code = review["failure_code"]

    def _payload(self, name: str) -> dict[str, object]:
        return self._payload_path(self.operations / name)

    def _payload_path(self, path: Path) -> dict[str, object]:
        return self._evidence_payload(self._evidence_document_path(path))

    def _evidence_document_path(self, path: Path) -> dict[str, object]:
        document = read_evidence(path)
        if (
            document.get("run_id") != self.run_id
            or document.get("project_id") != self.inputs.project_id
            or document.get("item_id") != self.inputs.package.item_id
            or document.get("scope_id") != self.inputs.package.scope_id
        ):
            raise WorkspaceError("operation evidence identity differs from write inputs")
        return document

    @staticmethod
    def _evidence_payload(document: dict[str, object]) -> dict[str, object]:
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise WorkspaceError("operation evidence payload is invalid")
        return payload

    def live_recheck(self, code: str) -> None:
        from agentgraph.integration import verify_work_source_revision

        current = self.git.snapshot(self.target)
        pinned = self.shadow.inspection.git_snapshot
        if (
            current.head_sha != self.inputs.baseline_head
            or current.branch != self.inputs.base_branch
            or current.detached_head
            or current.dirty
            or current.conflicted_paths
            or pinned.head_sha != self.inputs.baseline_head
        ):
            raise WriteBaselineDriftError(code)
        snapshot = self.source.snapshot()
        try:
            verify_work_source_revision(self.target.root, snapshot.revision)
        except Exception as exc:
            raise WriteBaselineDriftError(code) from exc
        if snapshot.revision.fingerprint != self.inputs.source_revision:
            raise WriteBaselineDriftError(code)

    def change_request(
        self,
        *,
        intent: ChangeIntent = ChangeIntent.IMPLEMENT,
        failure_context: RepairFailureContext | None = None,
    ) -> ChangeRequest:
        package = self.inputs.package
        explore = self.analysis.explore_analysis
        task_package = self.analysis.task_package
        effective_requirements, effective_acceptance = self.analysis.implementation_requirements()
        relevant_files = (
            ()
            if explore is None
            else tuple(
                dict.fromkeys(
                    (
                        *explore.relevant_files,
                        *(() if task_package is None else task_package.supporting_read_paths),
                    )
                )
            )
        )
        return ChangeRequest(
            self.inputs.project_id,
            package.item_id,
            package.scope_id,
            package.title,
            package.goal,
            package.acceptance_criteria,
            package.test_requirements,
            self.inputs.expected_allowed_paths,
            self.inputs.source_revision,
            self.inputs.baseline_head,
            (
                "external_runtime_worktree_only",
                "target_main_worktree_read_only",
                "one_item_bounded_repairs",
                "no_source_closure",
                "no_push_or_pull_request",
            ),
            () if explore is None else explore.architecture_observations,
            () if task_package is None else task_package.implementation_steps,
            () if task_package is None else task_package.validation_focus,
            () if explore is None else explore.derived_constraints,
            relevant_files,
            effective_requirements,
            effective_acceptance,
            intent,
            0 if failure_context is None else failure_context.cycle,
            None if failure_context is None else failure_context.failure_category,
            None if failure_context is None else failure_context.failure_code,
            None if failure_context is None else failure_context.failure_source_node,
            ()
            if failure_context is None
            else tuple(
                f"kind={item.kind.value} {item.command_id} "
                f"status={item.status} exit={item.exit_code} "
                f"stdout={item.stdout_preview!r} stderr={item.stderr_preview!r} "
                f"stdout_truncated={item.stdout_truncated} stderr_truncated={item.stderr_truncated}"
                for item in failure_context.validation_diagnostics
            ),
            () if failure_context is None else failure_context.review_findings,
            () if failure_context is None else failure_context.current_changed_paths,
            None if failure_context is None else failure_context.current_manifest_digest,
        )

    def prepare_implementation_analysis(self, state: GraphState) -> None:
        """Reconstruct advisory context from GraphState-bound immutable evidence."""

        self.analysis.prepare_implementation(state)

    def evidence_context(
        self,
        *,
        changeset_digest: str | None = None,
        repair_cycle: int | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "run_id": self.run_id,
            "project_id": self.inputs.project_id,
            "item_id": self.inputs.package.item_id,
            "scope_id": self.inputs.package.scope_id,
            "pinned_head": self.inputs.baseline_head,
            "source_revision": self.inputs.source_revision,
            "changeset_digest": (
                changeset_digest
                if changeset_digest is not None
                else None
                if self.changeset is None
                else self.changeset.digest
            ),
        }
        if repair_cycle is not None:
            result["repair_cycle"] = repair_cycle
        return result

    @staticmethod
    def _validation_diagnostic(
        kind: RepairValidationDiagnosticKind, receipt: CommandReceipt
    ) -> RepairValidationDiagnostic:
        return RepairValidationDiagnostic(
            kind,
            receipt.command_id,
            receipt.status.value,
            receipt.exit_code,
            receipt.stdout_preview,
            receipt.stderr_preview,
            receipt.stdout_truncated,
            receipt.stderr_truncated,
        )

    def _current_validation_diagnostics(self) -> tuple[RepairValidationDiagnostic, ...]:
        if self.diff_check is None:
            raise SemanticReviewContextError("semantic_review_context_mismatch")
        return (
            *(
                self._validation_diagnostic(
                    RepairValidationDiagnosticKind.DECLARED_VALIDATION, receipt
                )
                for receipt in self.validation_receipts
            ),
            self._validation_diagnostic(
                RepairValidationDiagnosticKind.GIT_DIFF_CHECK_WORKTREE,
                self.diff_check.receipts[0],
            ),
            self._validation_diagnostic(
                RepairValidationDiagnosticKind.GIT_DIFF_CHECK_STAGED,
                self.diff_check.receipts[1],
            ),
        )

    def _require_workspace(self) -> GitRepository:
        if self.workspace_repository is None:
            raise WorkspaceError("external workspace has not been created")
        return self.workspace_repository

    def _require_applied(self) -> AppliedChangeSet:
        if self.applied is None:
            raise WorkspaceError("changes have not been applied")
        return self.applied

    def _require_manifest(self) -> WorkspaceManifest:
        if self.manifest is None:
            raise WorkspaceError("workspace manifest has not been captured")
        return self.manifest

    def _cycle_evidence_path(self, cycle: int, name: str, *, create: bool = True) -> Path:
        if type(cycle) is not int or cycle < 0 or cycle > 2:
            raise WorkspaceError("repair cycle is outside the M009 bound")
        if cycle == 0:
            return self.operations / name
        directory = self.operations / "repairs" / f"{cycle:03d}"
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory / name


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _project_semantic_finding(finding) -> str:
    path = finding.path or "none"
    refs = ""
    if finding.requirement_refs:
        refs = f" [requirements: {', '.join(finding.requirement_refs)}]"
    return f"semantic:{finding.kind.value}:{path}:{finding.message}{refs}"


def _file_mode(path: Path) -> int:
    return 0o755 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o644


def _repository_semantics(snapshot) -> tuple[object, ...]:
    return (
        snapshot.head_sha,
        snapshot.branch,
        snapshot.detached_head,
        snapshot.staged_paths,
        snapshot.unstaged_paths,
        snapshot.untracked_paths,
        snapshot.conflicted_paths,
        snapshot.dirty,
    )


def _snapshot_evidence(snapshot) -> dict[str, object]:
    return {
        "head_sha": snapshot.head_sha,
        "branch": snapshot.branch,
        "detached_head": snapshot.detached_head,
        "staged_paths": tuple(path.as_posix() for path in snapshot.staged_paths),
        "unstaged_paths": tuple(path.as_posix() for path in snapshot.unstaged_paths),
        "untracked_paths": tuple(path.as_posix() for path in snapshot.untracked_paths),
        "conflicted_paths": tuple(path.as_posix() for path in snapshot.conflicted_paths),
        "dirty": snapshot.dirty,
    }
