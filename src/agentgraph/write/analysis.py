"""Durable guarded execution for M008 read-only analysis nodes."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentgraph.agents import (
    AGENT_RISK_ASSESSMENT_SCHEMA,
    AGENT_TASK_PACKAGE_SCHEMA,
    EXPLORE_ANALYSIS_SCHEMA,
    FAILURE_CLASSIFICATION_SCHEMA,
    SEMANTIC_REVIEW_SCHEMA,
    AgentAnalysisDriftError,
    AgentAnalysisStatus,
    AgentContext,
    AgentContextError,
    AgentEvidenceError,
    AgentMutationError,
    AgentProvider,
    AgentRequest,
    AgentResponse,
    AgentResponseContractError,
    AgentRiskAssessment,
    AgentTaskPackage,
    ExploreAnalysis,
    FailureClassificationAnalysis,
    SemanticReviewAnalysis,
    effective_risk,
    parse_explore_payload,
    parse_failure_classification_payload,
    parse_risk_payload,
    parse_semantic_review_payload,
    parse_task_package_payload,
    reconcile_explore,
    reconcile_task_package,
    stable_union,
)
from agentgraph.agents.prompts import build_semantic_review_prompt
from agentgraph.core import GraphState, RiskLevel
from agentgraph.infra import GitAdapter, GitRepository
from agentgraph.integration import verify_work_source_revision
from agentgraph.runtime.codec import encode_value, sha256_digest
from agentgraph.work import WorkSource

from .evidence import read_evidence, write_evidence
from .models import SemanticReviewContext, WriteInputs


def semantic_review_request(context: SemanticReviewContext) -> AgentRequest:
    """Build the one canonical request bound to an engine-owned review context."""

    return AgentRequest.create(
        "semantic_review",
        build_semantic_review_prompt(context),
        SEMANTIC_REVIEW_SCHEMA,
        "agentgraph.semantic-review.v1",
    )


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    response: AgentResponse
    value: (
        ExploreAnalysis
        | AgentTaskPackage
        | AgentRiskAssessment
        | FailureClassificationAnalysis
        | SemanticReviewAnalysis
    )
    evidence_reference: str


@dataclass(slots=True)
class AgentExecution:
    inputs: WriteInputs
    source: WorkSource
    provider: AgentProvider
    review_provider: AgentProvider | None
    git: GitAdapter
    target: GitRepository
    run_id: str
    run_path: Path
    source_root: Path | None = None
    guard_target: GitRepository | None = None
    explore_analysis: ExploreAnalysis | None = field(default=None, init=False)
    task_package: AgentTaskPackage | None = field(default=None, init=False)
    risk_assessment: AgentRiskAssessment | None = field(default=None, init=False)
    effective_requirements: tuple[str, ...] | None = field(default=None, init=False)
    effective_acceptance_criteria: tuple[str, ...] | None = field(default=None, init=False)
    issue_code: str | None = field(default=None, init=False)

    def explore(self, node_attempt_id: str, prompt: str) -> AnalysisResult:
        request = AgentRequest.create(
            "explore", prompt, EXPLORE_ANALYSIS_SCHEMA, "agentgraph.explore.v1"
        )
        result = self._invoke(
            "EXPLORE", node_attempt_id, request, parse_explore_payload, reconcile_explore
        )
        assert isinstance(result.value, ExploreAnalysis)
        self.explore_analysis = result.value
        return result

    def build_task_package(self, node_attempt_id: str, prompt: str) -> AnalysisResult:
        request = AgentRequest.create(
            "build_task_package",
            prompt,
            AGENT_TASK_PACKAGE_SCHEMA,
            "agentgraph.task-package.v1",
        )
        result = self._invoke(
            "BUILD_TASK_PACKAGE",
            node_attempt_id,
            request,
            parse_task_package_payload,
            lambda root, value: reconcile_task_package(root, self.inputs, value),
        )
        assert isinstance(result.value, AgentTaskPackage)
        self.task_package = result.value
        return result

    def assess_risk(
        self, node_attempt_id: str, prompt: str
    ) -> tuple[AnalysisResult, RiskLevel | None]:
        request = AgentRequest.create(
            "assess_risk",
            prompt,
            AGENT_RISK_ASSESSMENT_SCHEMA,
            "agentgraph.risk.v1",
        )
        result = self._invoke(
            "ASSESS_RISK",
            node_attempt_id,
            request,
            parse_risk_payload,
            lambda root, value: value,
        )
        assert isinstance(result.value, AgentRiskAssessment)
        self.risk_assessment = result.value
        if result.value.status is AgentAnalysisStatus.BLOCKED:
            return result, None
        return result, effective_risk(self.inputs.package.risk, result.value)

    def classify_failure(
        self,
        node_attempt_id: str,
        prompt: str,
        *,
        repository_root: Path,
        expected_workspace_digest: str,
        workspace_digest: Callable[[], str],
    ) -> AnalysisResult:
        request = AgentRequest.create(
            "classify_failure",
            prompt,
            FAILURE_CLASSIFICATION_SCHEMA,
            "agentgraph.failure-classification.v1",
        )
        result = self._invoke(
            "CLASSIFY_FAILURE",
            node_attempt_id,
            request,
            parse_failure_classification_payload,
            lambda root, value: value,
            repository_root=repository_root,
            expected_workspace_digest=expected_workspace_digest,
            workspace_digest=workspace_digest,
        )
        assert isinstance(result.value, FailureClassificationAnalysis)
        return result

    def semantic_review(
        self,
        node_attempt_id: str,
        context: SemanticReviewContext,
        *,
        repository_root: Path,
        expected_workspace_digest: str,
        workspace_digest: Callable[[], str],
    ) -> AnalysisResult:
        if self.review_provider is None:
            raise AgentContextError("review_provider_required")
        request = semantic_review_request(context)
        result = self._invoke(
            "REVIEW",
            node_attempt_id,
            request,
            parse_semantic_review_payload,
            _reconcile_semantic_review,
            provider=self.review_provider,
            repository_root=repository_root,
            expected_workspace_digest=expected_workspace_digest,
            workspace_digest=workspace_digest,
        )
        assert isinstance(result.value, SemanticReviewAnalysis)
        return result

    def restore_semantic_review(
        self,
        reference: Any,
        output_digest: Any,
        input_digest: Any,
    ) -> SemanticReviewAnalysis:
        result = self._restore(
            reference,
            output_digest,
            parse_semantic_review_payload,
            expected_node="REVIEW",
            expected_input_digest=input_digest,
            error_code="semantic_review_evidence_mismatch",
        )
        assert isinstance(result, SemanticReviewAnalysis)
        return result

    def prepare_implementation(self, state: GraphState) -> None:
        """Bind IMPLEMENT context to the exact durable GraphState and Explore evidence."""

        explore = self.restore_explore(state.baseline.metadata)
        self.restore_task_package(state.task_package.metadata)
        package = self.inputs.package
        requirements = stable_union(
            tuple(value for value in (package.goal, *package.test_requirements) if value),
            explore.derived_requirements,
        )
        acceptance = stable_union(
            package.acceptance_criteria,
            explore.derived_acceptance_criteria,
        )
        if (
            state.requirements.items != requirements
            or state.acceptance_criteria.items != acceptance
        ):
            raise AgentEvidenceError("agent_analysis_evidence_mismatch")
        self.effective_requirements = requirements
        self.effective_acceptance_criteria = acceptance

    def implementation_requirements(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if self.effective_requirements is None or self.effective_acceptance_criteria is None:
            raise AgentEvidenceError("agent_analysis_evidence_mismatch")
        return self.effective_requirements, self.effective_acceptance_criteria

    def restore_explore(self, metadata: Any) -> ExploreAnalysis:
        reference = metadata.get("agent_explore_evidence")
        digest = metadata.get("agent_explore_output_digest")
        result = self._restore(reference, digest, parse_explore_payload)
        assert isinstance(result, ExploreAnalysis)
        self.explore_analysis = result
        return result

    def restore_task_package(self, metadata: Any) -> AgentTaskPackage:
        reference = metadata.get("agent_task_package_evidence")
        digest = metadata.get("agent_task_package_output_digest")
        result = self._restore(reference, digest, parse_task_package_payload)
        assert isinstance(result, AgentTaskPackage)
        self.task_package = result
        return result

    def live_recheck(self) -> None:
        self._pinned_snapshot()

    def _invoke(
        self,
        node_id: str,
        node_attempt_id: str,
        request: AgentRequest,
        parser: Callable[[Any], Any],
        reconciler: Callable[[Path, Any], Any],
        *,
        repository_root: Path | None = None,
        expected_workspace_digest: str | None = None,
        workspace_digest: Callable[[], str] | None = None,
        provider: AgentProvider | None = None,
    ) -> AnalysisResult:
        selected_provider = provider or self.provider
        before = self._pinned_snapshot()
        selected_root = self.target.root if repository_root is None else repository_root
        if (expected_workspace_digest is None) is not (workspace_digest is None):
            raise AgentContextError("workspace manifest guard is incomplete")
        if workspace_digest is not None and workspace_digest() != expected_workspace_digest:
            raise AgentMutationError("repair_workspace_lineage_mismatch")
        provider_invocation_id, attempt_dir = self._attempt_directory(
            selected_provider, node_id, node_attempt_id
        )
        context = AgentContext(
            self.inputs.project_id,
            self.run_id,
            node_id,
            node_attempt_id,
            selected_root,
            attempt_dir,
            self.inputs.baseline_head,
            self.inputs.source_revision,
            provider_invocation_id,
        )
        provider_error: BaseException | None = None
        response: AgentResponse | None = None
        try:
            response = selected_provider.invoke(request, context)
        except BaseException as exc:
            provider_error = exc
        after = self.git.snapshot(self.target)
        if _repository_semantics(after) != _repository_semantics(before):
            self.issue_code = AgentMutationError.code
            raise AgentMutationError("agent_provider_mutated_repository") from provider_error
        if self.guard_target is not None:
            guarded = self.git.snapshot(self.guard_target)
            if not self._target_is_pinned(guarded):
                self.issue_code = AgentMutationError.code
                raise AgentMutationError("agent_provider_mutated_repository") from provider_error
        if workspace_digest is not None:
            try:
                workspace_unchanged = workspace_digest() == expected_workspace_digest
            except Exception as exc:
                self.issue_code = AgentMutationError.code
                raise AgentMutationError("agent_provider_mutated_repository") from (
                    provider_error or exc
                )
            if not workspace_unchanged:
                self.issue_code = AgentMutationError.code
                raise AgentMutationError("agent_provider_mutated_repository") from provider_error
        try:
            self._verify_source()
        except AgentAnalysisDriftError:
            self.issue_code = AgentAnalysisDriftError.code
            raise
        if provider_error is not None:
            raise provider_error
        if not isinstance(response, AgentResponse):
            raise AgentResponseContractError("AgentProvider returned a non-AgentResponse value")
        if response.input_digest != request.input_digest:
            raise AgentResponseContractError("agent response input digest mismatch")
        value = reconciler(selected_root, parser(dict(response.payload)))
        reference = self._persist_host_evidence(
            node_id,
            node_attempt_id,
            provider_invocation_id,
            attempt_dir,
            request,
            response,
            value,
        )
        return AnalysisResult(response, value, reference)

    def _persist_host_evidence(
        self,
        node_id: str,
        node_attempt_id: str,
        provider_invocation_id: str,
        attempt_dir: Path,
        request: AgentRequest,
        response: AgentResponse,
        value: Any,
    ) -> str:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        path = attempt_dir / "analysis.json"
        write_evidence(
            path,
            context={
                "project_id": self.inputs.project_id,
                "run_id": self.run_id,
                "node_id": node_id,
                "node_attempt_id": node_attempt_id,
                "provider_invocation_id": provider_invocation_id,
                "provider": response.provider_name,
                "provider_version": response.provider_version,
                "model": response.model,
                "input_digest": request.input_digest,
                "output_digest": response.output_digest,
                "source_revision": self.inputs.source_revision,
                "baseline_head": self.inputs.baseline_head,
            },
            payload={
                "response": encode_value(value),
                "provider_evidence": response.evidence_reference,
                "command_receipt": response.command_receipt,
            },
        )
        return path.relative_to(self.run_path).as_posix()

    def _restore(
        self,
        reference: Any,
        digest: Any,
        parser: Callable[[Any], Any],
        *,
        expected_node: str | None = None,
        expected_input_digest: Any = None,
        error_code: str = "agent_analysis_evidence_mismatch",
    ) -> Any:
        if not isinstance(reference, str) or not isinstance(digest, str):
            raise AgentEvidenceError(error_code)
        candidate = self.run_path.joinpath(*reference.split("/"))
        try:
            candidate.resolve(strict=True).relative_to(self.run_path.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise AgentEvidenceError(error_code) from exc
        try:
            document = read_evidence(candidate)
        except Exception as exc:
            raise AgentEvidenceError(error_code) from exc
        if (
            document.get("project_id") != self.inputs.project_id
            or document.get("run_id") != self.run_id
            or document.get("source_revision") != self.inputs.source_revision
            or document.get("baseline_head") != self.inputs.baseline_head
            or document.get("output_digest") != digest
            or (expected_node is not None and document.get("node_id") != expected_node)
            or (
                expected_input_digest is not None
                and document.get("input_digest") != expected_input_digest
            )
        ):
            raise AgentEvidenceError(error_code)
        payload = document.get("payload")
        if not isinstance(payload, dict) or "response" not in payload:
            raise AgentEvidenceError(error_code)
        try:
            result = parser(payload["response"])
        except AgentResponseContractError as exc:
            raise AgentEvidenceError(error_code) from exc
        if sha256_digest(encode_value(result)) != digest:
            raise AgentEvidenceError(error_code)
        return result

    def _attempt_directory(
        self, provider: AgentProvider, node_id: str, attempt_id: str
    ) -> tuple[str, Path]:
        namespace = getattr(provider, "evidence_namespace", "neutral")
        if (
            not isinstance(namespace, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", namespace) is None
        ):
            raise AgentContextError("agent evidence namespace is invalid")
        root = self.run_path / "provider" / namespace / "agents" / node_id
        root.mkdir(parents=True, exist_ok=True)
        run_root = self.run_path.resolve(strict=True)
        try:
            root.resolve(strict=True).relative_to(run_root)
        except ValueError as exc:
            raise AgentContextError("agent evidence directory escapes the durable run") from exc
        current = self.run_path
        for part in root.relative_to(self.run_path).parts:
            current /= part
            if current.is_symlink():
                raise AgentContextError("agent evidence directory cannot traverse a symlink")
        safe = sha256_digest(attempt_id).removeprefix("sha256:")[:16]
        selected = safe
        index = 1
        while (root / selected).exists() or (root / selected).is_symlink():
            index += 1
            selected = f"{safe}-rerun-{index}"
        return selected, root / selected

    def _pinned_snapshot(self):
        snapshot = self.git.snapshot(self.target)
        if (
            snapshot.head_sha != self.inputs.baseline_head
            or snapshot.branch != self.inputs.base_branch
            or snapshot.detached_head
            or snapshot.dirty
            or snapshot.conflicted_paths
        ):
            self.issue_code = AgentAnalysisDriftError.code
            raise AgentAnalysisDriftError("agent_analysis_baseline_drift")
        if self.guard_target is not None:
            target = self.git.snapshot(self.guard_target)
            if not self._target_is_pinned(target):
                self.issue_code = AgentAnalysisDriftError.code
                raise AgentAnalysisDriftError("agent_analysis_baseline_drift")
        self._verify_source()
        return snapshot

    def _target_is_pinned(self, snapshot) -> bool:
        return (
            snapshot.head_sha == self.inputs.pinned_target_head
            and snapshot.branch == self.inputs.pinned_target_branch
            and not snapshot.detached_head
            and not snapshot.dirty
            and not snapshot.conflicted_paths
        )

    def _verify_source(self) -> None:
        try:
            snapshot = self.source.snapshot()
            verify_work_source_revision(self.source_root or self.target.root, snapshot.revision)
        except Exception as exc:
            raise AgentAnalysisDriftError("agent_analysis_baseline_drift") from exc
        if snapshot.revision.fingerprint != self.inputs.source_revision:
            raise AgentAnalysisDriftError("agent_analysis_baseline_drift")


def _repository_semantics(snapshot: Any) -> tuple[object, ...]:
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


def _reconcile_semantic_review(root: Path, value: SemanticReviewAnalysis) -> SemanticReviewAnalysis:
    resolved_root = root.resolve(strict=True)
    for finding in value.findings:
        if finding.path is None:
            continue
        try:
            resolved_root.joinpath(*finding.path.split("/")).resolve(strict=False).relative_to(
                resolved_root
            )
        except ValueError as exc:
            raise AgentResponseContractError(
                "semantic review finding path escapes the repository"
            ) from exc
    return value
