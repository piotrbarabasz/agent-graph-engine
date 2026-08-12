"""Bounded typed results for the three M008 advisory roles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentgraph.core import RiskLevel
from agentgraph.runtime.codec import canonical_json_bytes

from .errors import AgentResponseContractError

MAX_PATHS = 100
MAX_LIST_ITEMS = 100
MAX_TEXT_LENGTH = 2000
MAX_PLAN_STEPS = 50
MAX_RESPONSE_BYTES = 256 * 1024
_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z", re.ASCII)


class AgentAnalysisStatus(StrEnum):
    SUCCESS = "success"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ExploreAnalysis:
    schema_version: int
    status: AgentAnalysisStatus
    relevant_files: tuple[str, ...]
    architecture_observations: tuple[str, ...]
    derived_requirements: tuple[str, ...]
    derived_acceptance_criteria: tuple[str, ...]
    derived_constraints: tuple[str, ...]
    architecture_invariants: tuple[str, ...]
    uncertainties: tuple[str, ...]
    reason_code: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class AgentTaskPackage:
    schema_version: int
    status: AgentAnalysisStatus
    objective: str | None
    implementation_steps: tuple[str, ...]
    recommended_change_paths: tuple[str, ...]
    supporting_read_paths: tuple[str, ...]
    validation_focus: tuple[str, ...]
    assumptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    reason_code: str | None
    message: str | None


@dataclass(frozen=True, slots=True)
class AgentRiskAssessment:
    schema_version: int
    status: AgentAnalysisStatus
    risk_level: RiskLevel | None
    reasons: tuple[str, ...]
    sensitive_areas: tuple[str, ...]
    destructive_change_concerns: tuple[str, ...]
    requests_human_checkpoint: bool
    reason_code: str | None
    message: str | None


def parse_explore_payload(value: Any) -> ExploreAnalysis:
    fields = {
        "schema_version",
        "status",
        "relevant_files",
        "architecture_observations",
        "derived_requirements",
        "derived_acceptance_criteria",
        "derived_constraints",
        "architecture_invariants",
        "uncertainties",
        "reason_code",
        "message",
    }
    document = _document(value, fields)
    status = _status(document)
    result = ExploreAnalysis(
        1,
        status,
        _strings(document, "relevant_files", MAX_PATHS),
        _strings(document, "architecture_observations"),
        _strings(document, "derived_requirements"),
        _strings(document, "derived_acceptance_criteria"),
        _strings(document, "derived_constraints"),
        _strings(document, "architecture_invariants"),
        _strings(document, "uncertainties"),
        document["reason_code"],
        document["message"],
    )
    _status_fields(status, result.reason_code, result.message)
    return result


def parse_task_package_payload(value: Any) -> AgentTaskPackage:
    fields = {
        "schema_version",
        "status",
        "objective",
        "implementation_steps",
        "recommended_change_paths",
        "supporting_read_paths",
        "validation_focus",
        "assumptions",
        "unresolved_questions",
        "reason_code",
        "message",
    }
    document = _document(value, fields)
    status = _status(document)
    objective = document["objective"]
    if objective is not None:
        _text(objective, "objective")
    result = AgentTaskPackage(
        1,
        status,
        objective,
        _strings(document, "implementation_steps", MAX_PLAN_STEPS),
        _strings(document, "recommended_change_paths", MAX_PATHS),
        _strings(document, "supporting_read_paths", MAX_PATHS),
        _strings(document, "validation_focus"),
        _strings(document, "assumptions"),
        _strings(document, "unresolved_questions"),
        document["reason_code"],
        document["message"],
    )
    _status_fields(status, result.reason_code, result.message)
    if status is AgentAnalysisStatus.SUCCESS and (
        not result.objective or not result.implementation_steps
    ):
        raise AgentResponseContractError("successful task package needs objective and plan")
    return result


def parse_risk_payload(value: Any) -> AgentRiskAssessment:
    fields = {
        "schema_version",
        "status",
        "risk_level",
        "reasons",
        "sensitive_areas",
        "destructive_change_concerns",
        "requests_human_checkpoint",
        "reason_code",
        "message",
    }
    document = _document(value, fields)
    status = _status(document)
    raw_risk = document["risk_level"]
    try:
        risk = None if raw_risk is None else RiskLevel(raw_risk)
    except (TypeError, ValueError) as exc:
        raise AgentResponseContractError("agent risk level is invalid") from exc
    checkpoint = document["requests_human_checkpoint"]
    if type(checkpoint) is not bool:
        raise AgentResponseContractError("checkpoint request must be boolean")
    result = AgentRiskAssessment(
        1,
        status,
        risk,
        _strings(document, "reasons"),
        _strings(document, "sensitive_areas"),
        _strings(document, "destructive_change_concerns"),
        checkpoint,
        document["reason_code"],
        document["message"],
    )
    _status_fields(status, result.reason_code, result.message)
    if status is AgentAnalysisStatus.SUCCESS and risk is None:
        raise AgentResponseContractError("successful risk assessment needs a risk level")
    return result


def _document(value: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise AgentResponseContractError("agent response has unknown or missing fields")
    try:
        encoded = canonical_json_bytes(value)
    except Exception as exc:
        raise AgentResponseContractError("agent response is not canonical JSON data") from exc
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise AgentResponseContractError("agent response exceeds the total size bound")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise AgentResponseContractError("unsupported agent response schema version")
    return value


def _status(document: dict[str, Any]) -> AgentAnalysisStatus:
    try:
        return AgentAnalysisStatus(document["status"])
    except (TypeError, ValueError) as exc:
        raise AgentResponseContractError("agent response status is invalid") from exc


def _strings(document: dict[str, Any], field: str, limit: int = MAX_LIST_ITEMS) -> tuple[str, ...]:
    values = document[field]
    if not isinstance(values, (list, tuple)) or len(values) > limit:
        raise AgentResponseContractError(f"{field} must be a bounded array")
    result = tuple(_text(value, field) for value in values)
    if len(set(result)) != len(result):
        raise AgentResponseContractError(f"{field} contains duplicate values")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > MAX_TEXT_LENGTH:
        raise AgentResponseContractError(f"{field} contains invalid text")
    return value


def _status_fields(status: AgentAnalysisStatus, reason_code: Any, message: Any) -> None:
    if status is AgentAnalysisStatus.SUCCESS:
        if reason_code is not None or message is not None:
            raise AgentResponseContractError("successful response has blocked fields")
        return
    if not isinstance(reason_code, str) or _REASON.fullmatch(reason_code) is None:
        raise AgentResponseContractError("blocked response reason code is invalid")
    if not isinstance(message, str) or not message or len(message) > MAX_TEXT_LENGTH:
        raise AgentResponseContractError("blocked response message is invalid")
