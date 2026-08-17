from __future__ import annotations

import pytest

from agentgraph.agents import (
    AgentAnalysisStatus,
    AgentResponseContractError,
    DeliveryReviewVerdict,
    parse_delivery_review_payload,
)


def _payload(**updates):
    value = {
        "schema_version": 1,
        "status": "success",
        "verdict": "pass",
        "summary": "Delivery is coherent.",
        "findings": [],
        "reason_code": None,
        "message": None,
    }
    value.update(updates)
    return value


def test_delivery_review_pass_fail_and_blocked_are_strictly_typed() -> None:
    passed = parse_delivery_review_payload(_payload())
    failed = parse_delivery_review_payload(
        _payload(
            verdict="fail",
            findings=[
                {
                    "kind": "cross_item_integration_failure",
                    "message": "Items do not integrate.",
                    "path": "src/example.py",
                    "item_ids": ["T001", "T002"],
                    "requirement_refs": ["scope behavior"],
                }
            ],
        )
    )
    blocked = parse_delivery_review_payload(
        _payload(
            status="blocked",
            verdict=None,
            summary=None,
            findings=[],
            reason_code="provider_blocked",
            message="provider unavailable",
        )
    )
    assert passed.verdict is DeliveryReviewVerdict.PASS
    assert failed.verdict is DeliveryReviewVerdict.FAIL
    assert blocked.status is AgentAnalysisStatus.BLOCKED


@pytest.mark.parametrize(
    "updates",
    (
        {"unknown": True},
        {"verdict": "pass", "findings": [{}]},
        {"verdict": "fail", "findings": []},
        {"status": "blocked", "verdict": "pass"},
        {
            "verdict": "fail",
            "findings": [
                {
                    "kind": "logic_defect",
                    "message": "bad path",
                    "path": "../escape.py",
                    "item_ids": [],
                    "requirement_refs": [],
                }
            ],
        },
    ),
)
def test_delivery_review_rejects_unknown_fields_incoherent_states_and_unsafe_paths(
    updates,
) -> None:
    with pytest.raises(AgentResponseContractError):
        parse_delivery_review_payload(_payload(**updates))
