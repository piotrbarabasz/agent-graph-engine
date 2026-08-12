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
    AgentAnalysisDriftError,
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
    effective_risk,
    parse_explore_payload,
    parse_risk_payload,
    parse_task_package_payload,
    reconcile_explore,
    reconcile_task_package,
)
from agentgraph.core import RiskLevel
from agentgraph.infra import GitAdapter, GitRepository
from agentgraph.integration import verify_work_source_revision
from agentgraph.runtime.codec import encode_value, sha256_digest
from agentgraph.work import WorkSource

from .evidence import read_evidence, write_evidence
from .models import WriteInputs


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    response: AgentResponse
    value: ExploreAnalysis | AgentTaskPackage | AgentRiskAssessment
    evidence_reference: str


@dataclass(slots=True)
class AgentExecution:
    inputs: WriteInputs
    source: WorkSource
    provider: AgentProvider
    git: GitAdapter
    target: GitRepository
    run_id: str
    run_path: Path
    explore_analysis: ExploreAnalysis | None = field(default=None, init=False)
    task_package: AgentTaskPackage | None = field(default=None, init=False)
    risk_assessment: AgentRiskAssessment | None = field(default=None, init=False)
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

    def assess_risk(self, node_attempt_id: str, prompt: str) -> tuple[AnalysisResult, RiskLevel]:
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
        return result, effective_risk(self.inputs.package.risk, result.value)

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
    ) -> AnalysisResult:
        before = self._pinned_snapshot()
        attempt_id, attempt_dir = self._attempt_directory(node_id, node_attempt_id)
        context = AgentContext(
            self.inputs.project_id,
            self.run_id,
            node_id,
            attempt_id,
            self.target.root,
            attempt_dir,
            self.inputs.baseline_head,
            self.inputs.source_revision,
        )
        provider_error: BaseException | None = None
        response: AgentResponse | None = None
        try:
            response = self.provider.invoke(request, context)
        except BaseException as exc:
            provider_error = exc
        after = self.git.snapshot(self.target)
        if _repository_semantics(after) != _repository_semantics(before):
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
        value = reconciler(self.target.root, parser(dict(response.payload)))
        reference = self._persist_host_evidence(
            node_id, attempt_id, attempt_dir, request, response, value
        )
        return AnalysisResult(response, value, reference)

    def _persist_host_evidence(
        self,
        node_id: str,
        attempt_id: str,
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
                "node_attempt_id": attempt_id,
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

    def _restore(self, reference: Any, digest: Any, parser: Callable[[Any], Any]) -> Any:
        if not isinstance(reference, str) or not isinstance(digest, str):
            raise AgentEvidenceError("agent_analysis_evidence_mismatch")
        candidate = self.run_path.joinpath(*reference.split("/"))
        try:
            candidate.resolve(strict=True).relative_to(self.run_path.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise AgentEvidenceError("agent_analysis_evidence_mismatch") from exc
        try:
            document = read_evidence(candidate)
        except Exception as exc:
            raise AgentEvidenceError("agent_analysis_evidence_mismatch") from exc
        if (
            document.get("project_id") != self.inputs.project_id
            or document.get("run_id") != self.run_id
            or document.get("source_revision") != self.inputs.source_revision
            or document.get("baseline_head") != self.inputs.baseline_head
            or document.get("output_digest") != digest
        ):
            raise AgentEvidenceError("agent_analysis_evidence_mismatch")
        payload = document.get("payload")
        if not isinstance(payload, dict) or "response" not in payload:
            raise AgentEvidenceError("agent_analysis_evidence_mismatch")
        try:
            result = parser(payload["response"])
        except AgentResponseContractError as exc:
            raise AgentEvidenceError("agent_analysis_evidence_mismatch") from exc
        if sha256_digest(encode_value(result)) != digest:
            raise AgentEvidenceError("agent_analysis_evidence_mismatch")
        return result

    def _attempt_directory(self, node_id: str, attempt_id: str) -> tuple[str, Path]:
        namespace = getattr(self.provider, "evidence_namespace", "neutral")
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
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", attempt_id)
        selected = safe
        index = 1
        while (root / selected).exists() or (root / selected).is_symlink():
            index += 1
            selected = f"{safe}-rerun-{index}"
        evidence_attempt = attempt_id if index == 1 else f"{attempt_id}-rerun-{index}"
        return evidence_attempt, root / selected

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
        self._verify_source()
        return snapshot

    def _verify_source(self) -> None:
        try:
            snapshot = self.source.snapshot()
            verify_work_source_revision(self.target.root, snapshot.revision)
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
