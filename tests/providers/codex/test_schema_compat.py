from __future__ import annotations

from collections.abc import Mapping

import pytest

from agentgraph.agents import (
    AGENT_RISK_ASSESSMENT_SCHEMA,
    AGENT_TASK_PACKAGE_SCHEMA,
    DELIVERY_REVIEW_SCHEMA,
    EXPLORE_ANALYSIS_SCHEMA,
    FAILURE_CLASSIFICATION_SCHEMA,
    SEMANTIC_REVIEW_SCHEMA,
)
from agentgraph.providers.codex import (
    CODEX_PROPOSAL_JSON_SCHEMA,
    CodexSchemaProjectionError,
    project_to_codex_supported_schema,
)

ALL_CODEX_SCHEMAS = {
    "explore": EXPLORE_ANALYSIS_SCHEMA,
    "task_package": AGENT_TASK_PACKAGE_SCHEMA,
    "risk": AGENT_RISK_ASSESSMENT_SCHEMA,
    "failure_classification": FAILURE_CLASSIFICATION_SCHEMA,
    "semantic_review": SEMANTIC_REVIEW_SCHEMA,
    "delivery_review": DELIVERY_REVIEW_SCHEMA,
    "proposal": CODEX_PROPOSAL_JSON_SCHEMA,
}
UNSUPPORTED_TRANSPORT_KEYWORDS = {
    "allOf",
    "if",
    "then",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
}


@pytest.mark.parametrize(("name", "schema"), ALL_CODEX_SCHEMAS.items())
def test_all_codex_schemas_project_to_strict_supported_structure(name, schema) -> None:
    projected = project_to_codex_supported_schema(schema)

    assert projected is not schema, name
    assert not _keys(projected).intersection(UNSUPPORTED_TRANSPORT_KEYWORDS), name
    _assert_objects_are_strict(projected)
    assert projected["required"] == schema["required"]
    assert set(projected["properties"]) == set(schema["properties"])


def test_projection_does_not_weaken_neutral_contracts() -> None:
    projected = project_to_codex_supported_schema(SEMANTIC_REVIEW_SCHEMA)

    assert SEMANTIC_REVIEW_SCHEMA["properties"]["findings"]["uniqueItems"] is True
    assert SEMANTIC_REVIEW_SCHEMA["properties"]["findings"]["maxItems"] > 0
    assert "allOf" in SEMANTIC_REVIEW_SCHEMA
    assert "uniqueItems" not in projected["properties"]["findings"]
    assert "maxItems" not in projected["properties"]["findings"]
    assert "allOf" not in projected


@pytest.mark.parametrize("keyword", ("pattern", "futureKeyword"))
def test_projection_fails_closed_for_future_unknown_keyword(keyword: str) -> None:
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string", keyword: True}},
        "required": ["value"],
        "additionalProperties": False,
    }

    with pytest.raises(CodexSchemaProjectionError, match=keyword):
        project_to_codex_supported_schema(schema)


def _keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value).union(*(_keys(child) for child in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_keys(child) for child in value))
    return set()


def _assert_objects_are_strict(value: object) -> None:
    if isinstance(value, Mapping):
        if value.get("type") == "object":
            assert value["additionalProperties"] is False
            assert set(value["required"]) == set(value["properties"])
        for child in value.values():
            _assert_objects_are_strict(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_objects_are_strict(child)
