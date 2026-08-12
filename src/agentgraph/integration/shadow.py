"""Bounded in-memory orchestration for a target-read-only graph probe."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from agentgraph.core import GraphEngine, PolicySnapshot, canonical_v1_graph
from agentgraph.infra import GitAdapter, RepositorySnapshot
from agentgraph.infra.errors import GitError, NotAGitRepositoryError
from agentgraph.nodes import (
    DiscoverProjectNode,
    FinalizeNode,
    PreflightNode,
    SelectWorkNode,
    StartNode,
)
from agentgraph.runtime import ProjectRegistry
from agentgraph.work import InvalidWorkSourceError, WorkSource, WorkSourceError

from .errors import RepositoryRootMismatchError, ShadowIntegrationError
from .inspection import inspect_project
from .models import (
    BranchDisposition,
    IntegrationIssue,
    IntegrationIssueSeverity,
    SelectionDisposition,
    ShadowInputs,
    ShadowOutcome,
    ShadowReport,
    ShadowRequest,
)
from .preflight import assess_preflight
from .selection import prepare_selection


class ShadowRunner:
    """Inspect once, exercise canonical deterministic nodes, and verify final drift."""

    def __init__(
        self,
        repository_root: Path | str,
        work_source: WorkSource,
        *,
        git_adapter: GitAdapter,
        project_registry: ProjectRegistry,
        policy: PolicySnapshot | None = None,
        run_id_factory: Callable[[str], str] | None = None,
        max_shadow_steps: int = 10,
    ) -> None:
        if max_shadow_steps < 1:
            raise ValueError("max_shadow_steps must be positive")
        self.repository_root = Path(repository_root)
        self.work_source = work_source
        self.git_adapter = git_adapter
        self.project_registry = project_registry
        self.policy = policy or PolicySnapshot()
        self.run_id_factory = run_id_factory or (
            lambda fingerprint: f"shadow_{fingerprint.removeprefix('sha256:')[:24]}"
        )
        self.max_shadow_steps = max_shadow_steps

    def run(self, request: ShadowRequest | None = None) -> ShadowReport:
        request = request or ShadowRequest()
        try:
            inspection = inspect_project(
                self.repository_root,
                git_adapter=self.git_adapter,
                project_registry=self.project_registry,
                work_source=self.work_source,
            )
        except RepositoryRootMismatchError as exc:
            return self._invalid_project("repository_root_mismatch", str(exc))
        except NotAGitRepositoryError as exc:
            return self._invalid_project("git_repository_missing", str(exc))
        except GitError as exc:
            return self._invalid_project("git_repository_invalid", str(exc))
        except InvalidWorkSourceError as exc:
            issues = tuple(
                IntegrationIssue(
                    item.code,
                    IntegrationIssueSeverity.ERROR,
                    item.message,
                    tuple(value for value in (item.scope_id, item.item_id) if value),
                )
                for item in exc.validation.issues
            )
            return ShadowReport(
                None,
                ShadowOutcome.INVALID_SOURCE,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                BranchDisposition.NOT_APPLICABLE,
                issues,
            )

        selection, package = prepare_selection(self.work_source, inspection.work_snapshot, request)
        preflight = assess_preflight(inspection, selection)
        fingerprint = _input_fingerprint(inspection, request)
        inputs = ShadowInputs(inspection, preflight, selection, package, fingerprint)
        nodes = {
            "START": StartNode(),
            "DISCOVER_PROJECT": DiscoverProjectNode(inputs),
            "PREFLIGHT": PreflightNode(inputs),
            "SELECT_WORK": SelectWorkNode(inputs),
            "FINALIZE": FinalizeNode(),
        }
        engine = GraphEngine(canonical_v1_graph(), self.policy, nodes)
        state = engine.initial_state(self.run_id_factory(fingerprint))
        executed: list[str] = []
        for _ in range(self.max_shadow_steps):
            if state.graph.current_node in {"EXPLORE", "END"}:
                break
            executed.append(state.graph.current_node)
            state, _, _ = engine.step(state)
        else:
            raise ShadowIntegrationError("shadow probe exceeded its deterministic step bound")

        outcome = _outcome(state.graph.current_node, preflight.ready, selection.disposition)
        issues = (*preflight.issues, *selection.issues)
        drift_issue = self._detect_drift(inspection.git_snapshot, inspection.work_snapshot.revision)
        if drift_issue is not None:
            outcome = ShadowOutcome.DRIFTED
            issues = (*issues, drift_issue)
        return ShadowReport(
            inspection.project_id,
            outcome,
            state,
            fingerprint,
            inspection.git_snapshot.head_sha,
            inspection.git_snapshot.branch,
            inspection.work_snapshot.revision.fingerprint,
            selection.scope_id,
            selection.item_id,
            package,
            preflight.branch_disposition,
            _unique_issues(issues),
            tuple(executed),
        )

    def _detect_drift(
        self, initial_git: RepositorySnapshot, initial_revision
    ) -> IntegrationIssue | None:
        try:
            final_git = self.git_adapter.snapshot(
                self.git_adapter.discover_repository(self.repository_root)
            )
            final_work = self.work_source.snapshot()
        except (GitError, WorkSourceError):
            return IntegrationIssue(
                "shadow_inputs_invalidated",
                IntegrationIssueSeverity.ERROR,
                "repository or work source became invalid during the probe",
            )
        if _git_identity(initial_git) != _git_identity(final_git):
            return IntegrationIssue(
                "repository_drift",
                IntegrationIssueSeverity.ERROR,
                "repository state changed during the probe",
            )
        if initial_revision.fingerprint != final_work.revision.fingerprint:
            return IntegrationIssue(
                "source_drift",
                IntegrationIssueSeverity.ERROR,
                "work source changed during the probe",
            )
        return None

    @staticmethod
    def _invalid_project(code: str, message: str) -> ShadowReport:
        issue = IntegrationIssue(code, IntegrationIssueSeverity.ERROR, message)
        return ShadowReport(
            None,
            ShadowOutcome.INVALID_PROJECT,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            BranchDisposition.NOT_APPLICABLE,
            (issue,),
        )


def _input_fingerprint(inspection, request: ShadowRequest) -> str:
    git = inspection.git_snapshot
    payload = {
        "project_id": inspection.project_id,
        "head_sha": git.head_sha,
        "branch": git.branch,
        "detached_head": git.detached_head,
        "staged_paths": [item.as_posix() for item in git.staged_paths],
        "unstaged_paths": [item.as_posix() for item in git.unstaged_paths],
        "untracked_paths": [item.as_posix() for item in git.untracked_paths],
        "conflicted_paths": [item.as_posix() for item in git.conflicted_paths],
        "work_source_revision": inspection.work_snapshot.revision.fingerprint,
        "scope_id": request.scope_id,
        "parent_scope_id": request.parent_scope_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _git_identity(snapshot: RepositorySnapshot) -> tuple[object, ...]:
    return (
        snapshot.head_sha,
        snapshot.branch,
        snapshot.detached_head,
        tuple(item.as_posix() for item in snapshot.staged_paths),
        tuple(item.as_posix() for item in snapshot.unstaged_paths),
        tuple(item.as_posix() for item in snapshot.untracked_paths),
        tuple(item.as_posix() for item in snapshot.conflicted_paths),
    )


def _outcome(
    cursor: str,
    preflight_ready: bool,
    selection: SelectionDisposition,
) -> ShadowOutcome:
    if cursor == "EXPLORE":
        return ShadowOutcome.READY_FOR_EXPLORE
    if not preflight_ready:
        return ShadowOutcome.BLOCKED
    if selection is SelectionDisposition.NO_WORK:
        return ShadowOutcome.NO_WORK
    if selection is SelectionDisposition.SELECTION_REQUIRED:
        return ShadowOutcome.SELECTION_REQUIRED
    return ShadowOutcome.BLOCKED


def _unique_issues(issues: tuple[IntegrationIssue, ...]) -> tuple[IntegrationIssue, ...]:
    seen: set[tuple[object, ...]] = set()
    values = []
    for issue in issues:
        key = (issue.code, issue.severity, issue.message, issue.related_ids)
        if key not in seen:
            seen.add(key)
            values.append(issue)
    return tuple(values)
