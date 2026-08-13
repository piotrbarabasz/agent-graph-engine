"""Strict JSON schemas for M008 analysis responses."""

from __future__ import annotations

from .analysis_models import (
    MAX_LIST_ITEMS,
    MAX_PATHS,
    MAX_PLAN_STEPS,
    MAX_REQUIREMENT_REFS,
    MAX_SEMANTIC_FINDINGS,
    MAX_SUMMARY_LENGTH,
    MAX_TEXT_LENGTH,
)


def _strings(max_items: int = MAX_LIST_ITEMS) -> dict[str, object]:
    return {
        "type": "array",
        "maxItems": max_items,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_LENGTH},
    }


def _root(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


_STATUS = {"enum": ["success", "blocked"]}
_REASON = {"type": ["string", "null"], "maxLength": 128}
_MESSAGE = {"type": ["string", "null"], "maxLength": MAX_TEXT_LENGTH}

EXPLORE_ANALYSIS_SCHEMA = _root(
    {
        "schema_version": {"const": 1},
        "status": _STATUS,
        "relevant_files": _strings(MAX_PATHS),
        "architecture_observations": _strings(),
        "derived_requirements": _strings(),
        "derived_acceptance_criteria": _strings(),
        "derived_constraints": _strings(),
        "architecture_invariants": _strings(),
        "uncertainties": _strings(),
        "reason_code": _REASON,
        "message": _MESSAGE,
    }
)

AGENT_TASK_PACKAGE_SCHEMA = _root(
    {
        "schema_version": {"const": 1},
        "status": _STATUS,
        "objective": {"type": ["string", "null"], "maxLength": MAX_TEXT_LENGTH},
        "implementation_steps": _strings(MAX_PLAN_STEPS),
        "recommended_change_paths": _strings(MAX_PATHS),
        "supporting_read_paths": _strings(MAX_PATHS),
        "validation_focus": _strings(),
        "assumptions": _strings(),
        "unresolved_questions": _strings(),
        "reason_code": _REASON,
        "message": _MESSAGE,
    }
)

AGENT_RISK_ASSESSMENT_SCHEMA = _root(
    {
        "schema_version": {"const": 1},
        "status": _STATUS,
        "risk_level": {"enum": ["low", "medium", "high", "critical", None]},
        "reasons": _strings(),
        "sensitive_areas": _strings(),
        "destructive_change_concerns": _strings(),
        "requests_human_checkpoint": {"type": "boolean"},
        "reason_code": _REASON,
        "message": _MESSAGE,
    }
)

FAILURE_CLASSIFICATION_SCHEMA = _root(
    {
        "schema_version": {"const": 1},
        "status": _STATUS,
        "classification": {"enum": ["programmer", "debugger", None]},
        "rationale": {"type": ["string", "null"], "maxLength": MAX_TEXT_LENGTH},
        "signals": _strings(),
        "reason_code": _REASON,
        "message": _MESSAGE,
    }
)
FAILURE_CLASSIFICATION_SCHEMA["allOf"] = [
    {
        "if": {"properties": {"status": {"const": "success"}}},
        "then": {
            "properties": {
                "classification": {"enum": ["programmer", "debugger"]},
                "rationale": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_LENGTH},
                "reason_code": {"type": "null"},
                "message": {"type": "null"},
            }
        },
    },
    {
        "if": {"properties": {"status": {"const": "blocked"}}},
        "then": {
            "properties": {
                "classification": {"type": "null"},
                "rationale": {"type": "null"},
                "reason_code": {"type": "string", "minLength": 1, "maxLength": 128},
                "message": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_LENGTH},
            }
        },
    },
]

SEMANTIC_REVIEW_SCHEMA = _root(
    {
        "schema_version": {"const": 1},
        "status": _STATUS,
        "verdict": {"enum": ["pass", "fail", None]},
        "summary": {"type": ["string", "null"], "maxLength": MAX_SUMMARY_LENGTH},
        "findings": {
            "type": "array",
            "maxItems": MAX_SEMANTIC_FINDINGS,
            "uniqueItems": True,
            "items": _root(
                {
                    "kind": {
                        "enum": [
                            "requirement_gap",
                            "acceptance_criterion_failure",
                            "architecture_violation",
                            "logic_defect",
                            "regression_risk",
                            "test_quality_issue",
                            "scope_violation",
                            "security_concern",
                            "maintainability_blocker",
                        ]
                    },
                    "path": {"type": ["string", "null"], "maxLength": MAX_TEXT_LENGTH},
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TEXT_LENGTH,
                    },
                    "requirement_refs": _strings(MAX_REQUIREMENT_REFS),
                }
            ),
        },
        "reason_code": _REASON,
        "message": _MESSAGE,
    }
)
SEMANTIC_REVIEW_SCHEMA["allOf"] = [
    {
        "if": {"properties": {"status": {"const": "blocked"}}},
        "then": {
            "properties": {
                "verdict": {"type": "null"},
                "summary": {"type": "null"},
                "findings": {"maxItems": 0},
                "reason_code": {"type": "string", "minLength": 1, "maxLength": 128},
                "message": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_LENGTH},
            }
        },
    },
    {
        "if": {"properties": {"status": {"const": "success"}, "verdict": {"const": "pass"}}},
        "then": {
            "properties": {
                "findings": {"maxItems": 0},
                "reason_code": {"type": "null"},
                "message": {"type": "null"},
            }
        },
    },
    {
        "if": {"properties": {"status": {"const": "success"}, "verdict": {"const": "fail"}}},
        "then": {
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": MAX_SUMMARY_LENGTH},
                "findings": {"minItems": 1},
                "reason_code": {"type": "null"},
                "message": {"type": "null"},
            }
        },
    },
]
