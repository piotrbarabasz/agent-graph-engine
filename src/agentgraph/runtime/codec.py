"""Strict deterministic JSON codec for core and runtime records."""

from __future__ import annotations

import hashlib
import json
import math
import types
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Union, get_args, get_origin, get_type_hints

from .errors import SerializationError


def encode_value(value: Any) -> Any:
    """Encode supported immutable DTO values into JSON-compatible primitives."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise SerializationError("non-finite floats are not JSON-compatible")
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: encode_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [encode_value(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise SerializationError("JSON mapping keys must be strings")
        return {key: encode_value(item) for key, item in value.items()}
    raise SerializationError(f"unsupported value type: {type(value).__name__}")


def decode_value(data: Any, expected_type: Any) -> Any:
    """Strictly decode JSON primitives into one caller-selected type."""

    try:
        return _decode(data, expected_type)
    except SerializationError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise SerializationError(str(exc)) from exc


def _decode(data: Any, expected_type: Any) -> Any:
    from agentgraph.core import OperationType, PatchOperation

    if expected_type is PatchOperation:
        return _decode_patch_operation(data, OperationType, PatchOperation)
    if expected_type is Any:
        _validate_json(data)
        return data
    origin = get_origin(expected_type)
    args = get_args(expected_type)
    if origin in {Union, types.UnionType}:
        errors = []
        for member in args:
            try:
                return _decode(data, member)
            except SerializationError as exc:
                errors.append(str(exc))
        raise SerializationError(f"value does not match union: {'; '.join(errors)}")
    if origin is tuple:
        if not isinstance(data, list):
            raise SerializationError("expected JSON array for tuple")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(item, args[0]) for item in data)
        if len(data) != len(args):
            raise SerializationError("fixed tuple length mismatch")
        return tuple(_decode(item, kind) for item, kind in zip(data, args, strict=True))
    if origin in {dict, Mapping}:
        if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
            raise SerializationError("expected JSON object with string keys")
        key_type, value_type = args or (str, Any)
        if key_type is not str:
            raise SerializationError("only string mapping keys are supported")
        return MappingProxyType({key: _decode(value, value_type) for key, value in data.items()})
    if isinstance(expected_type, type) and issubclass(expected_type, Enum):
        try:
            return expected_type(data)
        except ValueError as exc:
            raise SerializationError(f"unknown {expected_type.__name__} value") from exc
    if isinstance(expected_type, type) and is_dataclass(expected_type):
        if not isinstance(data, dict):
            raise SerializationError(f"expected object for {expected_type.__name__}")
        contract_fields = {field.name: field for field in fields(expected_type) if field.init}
        unknown = set(data) - set(contract_fields)
        if unknown:
            raise SerializationError(f"unknown {expected_type.__name__} fields: {sorted(unknown)}")
        hints = get_type_hints(expected_type)
        values = {}
        for name, field in contract_fields.items():
            if name in data:
                values[name] = _decode(data[name], hints[name])
            elif field.default is MISSING and field.default_factory is MISSING:
                raise SerializationError(f"missing required field {expected_type.__name__}.{name}")
        return expected_type(**values)
    if expected_type is type(None):
        if data is not None:
            raise SerializationError("expected null")
        return None
    if expected_type in {bool, int, float, str}:
        if expected_type is float:
            if type(data) not in {int, float} or type(data) is bool or not math.isfinite(data):
                raise SerializationError("expected finite number")
            return float(data)
        if type(data) is not expected_type:
            raise SerializationError(f"expected {expected_type.__name__}")
        return data
    raise SerializationError(f"unsupported expected type: {expected_type!r}")


def _decode_patch_operation(data: Any, operation_type: Any, patch_type: Any) -> Any:
    from agentgraph.core.patches import PATH_RULES

    if not isinstance(data, dict) or set(data) != {"operation", "path", "value"}:
        raise SerializationError("invalid PatchOperation fields")
    operation = _decode(data["operation"], operation_type)
    path = _decode(data["path"], str)
    rule = PATH_RULES.get(path)
    if rule is None:
        raise SerializationError(f"unknown patch path: {path}")
    raw = data["value"]
    if operation is operation_type.CLEAR:
        value = _decode(raw, type(None))
    elif operation is operation_type.INCREMENT:
        value = _decode(raw, int)
    elif operation is operation_type.SET:
        if raw is None and rule.nullable:
            value = None
        elif tuple in rule.value_types:
            if not isinstance(raw, list):
                raise SerializationError("tuple patch value must be an array")
            value = tuple(_decode(item, rule.item_types[0]) for item in raw)
        elif any(kind.__name__ == "Mapping" for kind in rule.value_types):
            _validate_json(raw)
            if not isinstance(raw, dict):
                raise SerializationError("mapping patch value must be an object")
            value = MappingProxyType(raw)
        else:
            value = _decode(raw, rule.value_types[0])
    else:
        value = _decode(raw, rule.item_types[0])
    return patch_type(operation, path, value)


def _validate_json(value: Any) -> None:
    encode_value(value)


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical UTF-8 JSON bytes for values or DTOs."""

    encoded = encode_value(value)
    return json.dumps(
        encoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def parse_json_bytes(data: bytes) -> Any:
    """Parse UTF-8 JSON while rejecting duplicate object keys and non-standard constants."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SerializationError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                SerializationError(f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SerializationError("malformed UTF-8 JSON") from exc


def sha256_digest(value: Any) -> str:
    """Return a stable prefixed SHA-256 digest of canonical JSON."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    """Encode an aware timestamp in stable UTC ISO-8601 form."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise SerializationError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime:
    """Decode and validate a persisted UTC timestamp."""

    if not isinstance(value, str) or not value.endswith("Z"):
        raise SerializationError("timestamp must be an ISO-8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SerializationError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise SerializationError("timestamp must use UTC")
    return parsed
