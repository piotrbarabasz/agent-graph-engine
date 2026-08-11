from dataclasses import replace

import pytest

from agentgraph.core import (
    FailureCategory,
    GraphState,
    NodeResult,
    NodeStatus,
    PatchOperation,
    ResultReason,
    RiskLevel,
    StatePatch,
)
from agentgraph.core.state import ProjectState
from agentgraph.runtime.codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
    parse_json_bytes,
    sha256_digest,
)
from agentgraph.runtime.errors import SerializationError


def roundtrip(value, kind):
    encoded = parse_json_bytes(canonical_json_bytes(value))
    return decode_value(encoded, kind)


def test_graph_state_roundtrip_preserves_types_and_equality() -> None:
    state = replace(
        GraphState.initial("run-1", max_repair_cycles=2),
        project=ProjectState("demo", {"nested": [1, {"ok": True}]}),
    )
    restored = roundtrip(state, GraphState)
    assert restored == state
    assert isinstance(restored.project.metadata["nested"], tuple)


def test_node_result_and_typed_patch_roundtrip() -> None:
    result = NodeResult(
        "ASSESS_RISK",
        "attempt",
        NodeStatus.FAILED,
        reason=ResultReason("risk", "failed"),
        failure_category=FailureCategory.INTERNAL,
        state_patch=StatePatch(0, (PatchOperation.set("risk.level", RiskLevel.HIGH),)),
    )
    restored = roundtrip(result, NodeResult)
    assert restored == result
    assert restored.state_patch.operations[0].value is RiskLevel.HIGH


@pytest.mark.parametrize("value", [b"bytes", object(), lambda: None])
def test_arbitrary_metadata_values_are_rejected(value: object) -> None:
    with pytest.raises(SerializationError):
        encode_value({"invalid": value})


def test_unknown_fields_enum_and_required_fields_fail_closed() -> None:
    data = encode_value(GraphState.initial("r"))
    with pytest.raises(SerializationError, match="unknown"):
        decode_value({**data, "surprise": True}, GraphState)
    data["run"]["status"] = "not-a-status"
    with pytest.raises(SerializationError, match="unknown RunStatus"):
        decode_value(data, GraphState)
    with pytest.raises(SerializationError, match="missing required"):
        decode_value({}, GraphState)


def test_canonical_digest_is_order_independent() -> None:
    assert sha256_digest({"b": 2, "a": 1}) == sha256_digest({"a": 1, "b": 2})
