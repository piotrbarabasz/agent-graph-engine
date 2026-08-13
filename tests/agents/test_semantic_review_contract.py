from __future__ import annotations

import pytest

from agentgraph.agents import (
    AgentAnalysisStatus,
    AgentResponseContractError,
    SemanticReviewFindingKind,
    SemanticReviewVerdict,
    parse_semantic_review_payload,
)


def payload(**overrides):
    value = {
        "schema_version": 1,
        "status": "success",
        "verdict": "pass",
        "summary": "The current change satisfies the task.",
        "findings": [],
        "reason_code": None,
        "message": None,
    }
    value.update(overrides)
    return value


def test_semantic_pass_and_fail_are_typed() -> None:
    passed = parse_semantic_review_payload(payload())
    failed = parse_semantic_review_payload(
        payload(
            verdict="fail",
            findings=[
                {
                    "kind": "logic_defect",
                    "path": "src/service.py",
                    "message": "The changed branch returns the wrong value.",
                    "requirement_refs": ["REQ-1"],
                }
            ],
        )
    )

    assert passed.status is AgentAnalysisStatus.SUCCESS
    assert passed.verdict is SemanticReviewVerdict.PASS
    assert failed.verdict is SemanticReviewVerdict.FAIL
    assert failed.findings[0].kind is SemanticReviewFindingKind.LOGIC_DEFECT


@pytest.mark.parametrize(
    "value",
    [
        payload(safe_to_close=True),
        payload(
            findings=[
                {
                    "kind": "logic_defect",
                    "path": "../escape.py",
                    "message": "bad",
                    "requirement_refs": [],
                }
            ]
        ),
        payload(
            findings=[
                {
                    "kind": "logic_defect",
                    "path": "C:/escape.py",
                    "message": "bad",
                    "requirement_refs": [],
                }
            ]
        ),
        payload(
            findings=[
                {
                    "kind": "logic_defect",
                    "path": "/absolute.py",
                    "message": "bad",
                    "requirement_refs": [],
                }
            ]
        ),
        payload(
            verdict="pass",
            findings=[
                {
                    "kind": "logic_defect",
                    "path": None,
                    "message": "bad",
                    "requirement_refs": [],
                }
            ],
        ),
        payload(verdict="fail", findings=[]),
        payload(status="blocked", verdict=None, summary=None, reason_code=None, message="blocked"),
    ],
)
def test_semantic_contract_rejects_control_fields_paths_and_incoherent_states(value) -> None:
    with pytest.raises(AgentResponseContractError):
        parse_semantic_review_payload(value)


def test_blocked_contract_has_no_decision() -> None:
    result = parse_semantic_review_payload(
        payload(
            status="blocked",
            verdict=None,
            summary=None,
            findings=[],
            reason_code="review_evidence_unavailable",
            message="The repository cannot be inspected safely.",
        )
    )

    assert result.status is AgentAnalysisStatus.BLOCKED
    assert result.verdict is None


def test_semantic_finding_count_is_bounded() -> None:
    finding = {
        "kind": "logic_defect",
        "path": None,
        "message": "material defect",
        "requirement_refs": [],
    }
    with pytest.raises(AgentResponseContractError):
        parse_semantic_review_payload(payload(verdict="fail", findings=[finding] * 21))
