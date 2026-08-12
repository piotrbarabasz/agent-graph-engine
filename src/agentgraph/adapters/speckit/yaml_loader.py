"""Safe YAML loading with strict duplicate mapping-key rejection."""

from __future__ import annotations

from typing import Any

import yaml

from agentgraph.work import WorkSourceFormatError


class DuplicateKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant rejecting ambiguous duplicate mapping keys."""


def _construct_mapping(
    loader: DuplicateKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise WorkSourceFormatError("YAML mapping key must be hashable") from exc
        if duplicate:
            raise WorkSourceFormatError(f"duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


def load_yaml(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkSourceFormatError("YAML source must be UTF-8") from exc
    try:
        return yaml.load(text, Loader=DuplicateKeySafeLoader)
    except WorkSourceFormatError:
        raise
    except yaml.YAMLError as exc:
        raise WorkSourceFormatError("malformed YAML source") from exc
