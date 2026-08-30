"""Fail-closed projection from neutral contracts to Codex transport schemas."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .errors import CodexSchemaProjectionError

_SIMPLE_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "additionalProperties",
        "$ref",
        "description",
    }
)
_SCHEMA_KEYWORDS = frozenset({"items"})
_SCHEMA_ARRAY_KEYWORDS = frozenset({"anyOf"})
_SCHEMA_MAPPING_KEYWORDS = frozenset({"properties", "$defs"})
_NAME_ARRAY_KEYWORDS = frozenset({"required"})
_CODEX_KEYWORDS = (
    _SIMPLE_KEYWORDS
    | _SCHEMA_KEYWORDS
    | _SCHEMA_ARRAY_KEYWORDS
    | _SCHEMA_MAPPING_KEYWORDS
    | _NAME_ARRAY_KEYWORDS
)

# These remain authoritative in the provider-neutral parser. They are intentionally
# absent from the smaller Structured Outputs vocabulary sent to Codex.
_LOCAL_VALIDATION_KEYWORDS = frozenset(
    {
        "allOf",
        "if",
        "then",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
    }
)
_JSON_TYPES = frozenset({"null", "boolean", "object", "array", "number", "integer", "string"})


def project_to_codex_supported_schema(schema: Any) -> dict[str, Any]:
    """Return a strict Codex-compatible transport schema.

    AgentGraph's original schema is not mutated. Known domain-validation keywords
    stay local, while unknown keywords fail closed so a future neutral contract
    cannot accidentally broaden the model-facing schema.
    """

    if not isinstance(schema, Mapping):
        raise CodexSchemaProjectionError("Codex output schema must be an object")
    return _project_schema(schema, location="$", require_strict_object=True)


def _project_schema(
    schema: Mapping[object, object], *, location: str, require_strict_object: bool = False
) -> dict[str, Any]:
    if not all(isinstance(key, str) for key in schema):
        raise CodexSchemaProjectionError(f"Codex output schema has a non-string key at {location}")
    unknown = set(schema) - _CODEX_KEYWORDS - _LOCAL_VALIDATION_KEYWORDS
    if unknown:
        keyword = min(unknown)
        raise CodexSchemaProjectionError(
            f"Codex output schema uses an unrecognized keyword at {location}: {keyword}"
        )

    projected: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _LOCAL_VALIDATION_KEYWORDS:
            continue
        child_location = f"{location}.{key}"
        if key in _SCHEMA_MAPPING_KEYWORDS:
            projected[key] = _project_schema_mapping(value, location=child_location)
        elif key in _SCHEMA_KEYWORDS:
            projected[key] = _require_and_project_schema(value, location=child_location)
        elif key in _SCHEMA_ARRAY_KEYWORDS:
            projected[key] = _project_schema_array(value, location=child_location)
        elif key in _NAME_ARRAY_KEYWORDS:
            projected[key] = _project_name_array(value, location=child_location)
        else:
            projected[key] = _project_simple(key, value, location=child_location)

    _normalize_scalar_constraints(projected, location=location)
    _validate_projected_schema(
        projected, location=location, require_strict_object=require_strict_object
    )
    return projected


def _project_schema_mapping(value: object, *, location: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) and key for key in value):
        raise CodexSchemaProjectionError(f"Codex output schema mapping is invalid at {location}")
    return {
        key: _require_and_project_schema(child, location=f"{location}.{key}")
        for key, child in value.items()
    }


def _require_and_project_schema(value: object, *, location: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CodexSchemaProjectionError(f"Codex output subschema is invalid at {location}")
    return _project_schema(value, location=location)


def _project_schema_array(value: object, *, location: str) -> list[dict[str, Any]]:
    if not _is_sequence(value) or not value:
        raise CodexSchemaProjectionError(
            f"Codex output schema alternatives are invalid at {location}"
        )
    return [
        _require_and_project_schema(child, location=f"{location}[{index}]")
        for index, child in enumerate(value)
    ]


def _project_name_array(value: object, *, location: str) -> list[str]:
    if (
        not _is_sequence(value)
        or not all(isinstance(item, str) and item for item in value)
        or len(set(value)) != len(value)
    ):
        raise CodexSchemaProjectionError(f"Codex output schema names are invalid at {location}")
    return list(value)


def _project_simple(key: str, value: object, *, location: str) -> Any:
    if key == "type":
        return _project_type(value, location=location)
    if key == "additionalProperties":
        if value is not False:
            raise CodexSchemaProjectionError(
                f"Codex output objects must forbid additional properties at {location}"
            )
        return False
    if key == "enum":
        if not _is_sequence(value) or not value:
            raise CodexSchemaProjectionError(f"Codex output enum is invalid at {location}")
        return list(value)
    if key == "const":
        return value
    if key in {"$ref", "description"}:
        if not isinstance(value, str) or not value:
            raise CodexSchemaProjectionError(f"Codex output schema text is invalid at {location}")
        return value
    raise AssertionError(f"unhandled Codex schema keyword: {key}")


def _project_type(value: object, *, location: str) -> str | list[str]:
    if isinstance(value, str):
        if value not in _JSON_TYPES:
            raise CodexSchemaProjectionError(f"Codex output type is invalid at {location}")
        return value
    if (
        not _is_sequence(value)
        or not all(isinstance(item, str) and item in _JSON_TYPES for item in value)
        or len(set(value)) != len(value)
        or len(value) != 2
        or "null" not in value
    ):
        raise CodexSchemaProjectionError(
            f"Codex output type union must be a single nullable type at {location}"
        )
    return list(value)


def _normalize_scalar_constraints(schema: dict[str, Any], *, location: str) -> None:
    if "const" in schema:
        if "enum" in schema:
            raise CodexSchemaProjectionError(
                f"Codex output schema cannot combine const and enum at {location}"
            )
        schema["enum"] = [schema.pop("const")]
    if "enum" not in schema:
        return

    values = schema["enum"]
    inferred_type = _infer_enum_type(values, location=location)
    declared_type = schema.get("type")
    if declared_type is None:
        schema["type"] = inferred_type
        return

    allowed_types = {declared_type} if isinstance(declared_type, str) else set(declared_type)
    for value in values:
        value_type = _json_scalar_type(value, location=location)
        if value_type in allowed_types:
            continue
        if value_type == "integer" and "number" in allowed_types:
            continue
        raise CodexSchemaProjectionError(
            f"Codex output enum is inconsistent with its explicit type at {location}"
        )


def _infer_enum_type(values: list[Any], *, location: str) -> str | list[str]:
    value_types = {_json_scalar_type(value, location=location) for value in values}
    nullable = "null" in value_types
    non_null_types = value_types - {"null"}
    if not non_null_types:
        return "null"
    if non_null_types == {"integer", "number"}:
        scalar_type = "number"
    elif len(non_null_types) == 1:
        scalar_type = next(iter(non_null_types))
    else:
        raise CodexSchemaProjectionError(
            f"Codex output enum has incompatible scalar types at {location}"
        )
    return [scalar_type, "null"] if nullable else scalar_type


def _json_scalar_type(value: object, *, location: str) -> str:
    value_type = type(value)
    if value is None:
        return "null"
    if value_type is str:
        return "string"
    if value_type is bool:
        return "boolean"
    if value_type is int:
        return "integer"
    if value_type is float and math.isfinite(value):
        return "number"
    raise CodexSchemaProjectionError(f"Codex output enum has a non-JSON scalar at {location}")


def _validate_projected_schema(
    schema: Mapping[str, Any], *, location: str, require_strict_object: bool
) -> None:
    is_object = schema.get("type") == "object" or "properties" in schema
    if require_strict_object and not is_object:
        raise CodexSchemaProjectionError("Codex output schema root must be a strict object")
    if is_object:
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, dict)
            or schema.get("additionalProperties") is not False
            or not isinstance(required, list)
            or set(required) != set(properties)
            or len(required) != len(properties)
        ):
            raise CodexSchemaProjectionError(
                f"Codex output object must require exactly its declared properties at {location}"
            )
    if schema.get("type") == "array" and "items" not in schema:
        raise CodexSchemaProjectionError(f"Codex output array has no item schema at {location}")


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
