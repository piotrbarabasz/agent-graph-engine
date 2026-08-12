"""Engine-owned reconciliation of advisory agent output."""

from __future__ import annotations

from pathlib import Path

from agentgraph.core import RiskLevel
from agentgraph.work import WorkRisk
from agentgraph.write import normalize_repo_path, path_is_allowed
from agentgraph.write.models import WriteInputs

from .analysis_models import AgentRiskAssessment, AgentTaskPackage, ExploreAnalysis
from .errors import AgentResponseContractError, AgentScopeExpansionError

_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def reconcile_explore(root: Path, value: ExploreAnalysis) -> ExploreAnalysis:
    _validate_read_paths(root, value.relevant_files)
    return value


def reconcile_task_package(
    root: Path, inputs: WriteInputs, value: AgentTaskPackage
) -> AgentTaskPackage:
    _validate_read_paths(root, value.supporting_read_paths)
    for path in value.recommended_change_paths:
        normalize_repo_path(path)
        if not path_is_allowed(path, inputs.expected_allowed_paths):
            raise AgentScopeExpansionError("agent_task_package_scope_expansion")
    return value


def effective_risk(source: WorkRisk, agent: AgentRiskAssessment) -> RiskLevel:
    source_level = RiskLevel(source.value)
    if agent.risk_level is None:
        raise AgentResponseContractError("agent risk assessment is missing a risk level")
    return max((source_level, agent.risk_level), key=_RISK_ORDER.__getitem__)


def stable_union(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for group in groups for value in group if value))


def _validate_read_paths(root: Path, values: tuple[str, ...]) -> None:
    canonical = root.resolve(strict=True)
    for value in values:
        pure = normalize_repo_path(value)
        candidate = canonical.joinpath(*pure.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(canonical)
        except (OSError, ValueError) as exc:
            raise AgentResponseContractError("agent read path escapes or is absent") from exc
        if candidate.is_symlink() or not (resolved.is_file() or resolved.is_dir()):
            raise AgentResponseContractError("agent read path is not a regular file or directory")
