from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.agents import (
    EXPLORE_ANALYSIS_SCHEMA,
    AgentRequest,
    AgentResponse,
    AgentResponseContractError,
    parse_explore_payload,
)
from agentgraph.runtime.codec import sha256_digest


def explore_payload(**updates):
    value = {
        "schema_version": 1,
        "status": "success",
        "relevant_files": [],
        "architecture_observations": [],
        "derived_requirements": [],
        "derived_acceptance_criteria": [],
        "derived_constraints": [],
        "architecture_invariants": [],
        "uncertainties": [],
        "reason_code": None,
        "message": None,
    }
    value.update(updates)
    return value


def test_agent_request_digest_is_deterministic_and_tamper_evident() -> None:
    first = AgentRequest.create("explore", "prompt", EXPLORE_ANALYSIS_SCHEMA, "schema.v1")
    second = AgentRequest.create("explore", "prompt", EXPLORE_ANALYSIS_SCHEMA, "schema.v1")

    assert first.input_digest == second.input_digest
    with pytest.raises(AgentResponseContractError):
        AgentRequest("explore", "changed", EXPLORE_ANALYSIS_SCHEMA, "schema.v1", first.input_digest)


def test_agent_response_digest_is_engine_verifiable() -> None:
    payload = explore_payload()

    with pytest.raises(AgentResponseContractError, match="digest"):
        AgentResponse(
            payload,
            "fake",
            "1",
            None,
            sha256_digest({"request": 1}),
            sha256_digest({"different": True}),
            "response.json",
        )


@pytest.mark.parametrize(
    "updates",
    (
        {"next_node": "IMPLEMENT"},
        {"relevant_files": ["src/a.py", "src/a.py"]},
        {"architecture_observations": ["x" * 2001]},
        {"relevant_files": [f"src/{index}.py" for index in range(101)]},
    ),
)
def test_explore_contract_rejects_control_fields_duplicates_and_bounds(updates) -> None:
    with pytest.raises(AgentResponseContractError):
        parse_explore_payload(explore_payload(**updates))


def test_explore_contract_rejects_oversized_total_structured_response() -> None:
    payload = explore_payload(
        architecture_observations=[f"{index:03d}-{'x' * 1990}" for index in range(100)],
        derived_requirements=[f"{index:03d}-{'y' * 1990}" for index in range(100)],
    )

    with pytest.raises(AgentResponseContractError, match="total size"):
        parse_explore_payload(payload)


def test_agent_contract_module_has_no_codex_coupling() -> None:
    root = Path(__file__).parents[2] / "src" / "agentgraph" / "agents"
    assert "providers.codex" not in "\n".join(
        path.read_text(encoding="utf-8") for path in root.glob("*.py")
    )
