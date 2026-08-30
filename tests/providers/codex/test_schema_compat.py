from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

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
    assert "const" not in _keys(projected), name
    _assert_transport_invariants(projected)
    assert projected["required"] == schema["required"]
    assert set(projected["properties"]) == set(schema["properties"])


@pytest.mark.parametrize(
    ("neutral", "expected"),
    (
        ({"const": 1}, {"type": "integer", "enum": [1]}),
        (
            {"enum": ["success", "blocked"]},
            {"type": "string", "enum": ["success", "blocked"]},
        ),
        (
            {"enum": ["low", "medium", None]},
            {"type": ["string", "null"], "enum": ["low", "medium", None]},
        ),
        ({"const": True}, {"type": "boolean", "enum": [True]}),
        ({"enum": [True, False]}, {"type": "boolean", "enum": [True, False]}),
        ({"enum": [1, 2]}, {"type": "integer", "enum": [1, 2]}),
        ({"const": 1.5}, {"type": "number", "enum": [1.5]}),
        ({"enum": [1, 2.5]}, {"type": "number", "enum": [1, 2.5]}),
        ({"const": None}, {"type": "null", "enum": [None]}),
        (
            {"enum": [1, 2, None]},
            {"type": ["integer", "null"], "enum": [1, 2, None]},
        ),
    ),
)
def test_scalar_constraints_gain_explicit_codex_types(neutral, expected) -> None:
    assert _project_fragment(neutral) == expected


def test_projection_fails_closed_for_heterogeneous_enum() -> None:
    with pytest.raises(CodexSchemaProjectionError, match="incompatible scalar types"):
        _project_fragment({"enum": ["success", 1]})


@pytest.mark.parametrize(
    "fragment",
    (
        {"type": "integer", "enum": ["one", "two"]},
        {"type": "string", "const": 1},
    ),
)
def test_projection_fails_closed_for_constraint_inconsistent_with_explicit_type(fragment) -> None:
    with pytest.raises(CodexSchemaProjectionError, match="inconsistent with its explicit type"):
        _project_fragment(fragment)


def test_scalar_normalization_does_not_mutate_neutral_schema() -> None:
    neutral = {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1},
            "risk": {"enum": ["low", "high", None]},
        },
        "required": ["schema_version", "risk"],
        "additionalProperties": False,
    }
    original = deepcopy(neutral)

    projected = project_to_codex_supported_schema(neutral)

    assert neutral == original
    assert projected["properties"]["schema_version"] == {"type": "integer", "enum": [1]}
    assert projected["properties"]["risk"] == {
        "type": ["string", "null"],
        "enum": ["low", "high", None],
    }


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


def _project_fragment(fragment: dict[str, object]) -> dict[str, object]:
    schema = {
        "type": "object",
        "properties": {"value": fragment},
        "required": ["value"],
        "additionalProperties": False,
    }
    return project_to_codex_supported_schema(schema)["properties"]["value"]


def _keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value).union(*(_keys(child) for child in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_keys(child) for child in value))
    return set()


def _assert_transport_invariants(value: object) -> None:
    if isinstance(value, Mapping):
        if value.get("type") == "object":
            assert value["additionalProperties"] is False
            assert set(value["required"]) == set(value["properties"])
        if value.get("type") == "array":
            assert "items" in value
        if "enum" in value:
            assert "type" in value
            if None in value["enum"]:
                enum_type = value["type"]
                assert enum_type == "null" or (
                    isinstance(enum_type, list) and "null" in enum_type
                )
        for child in value.values():
            _assert_transport_invariants(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_transport_invariants(child)
