import pytest

from agentgraph.core import (
    CheckpointRequest,
    ContractValidationError,
    FailureCategory,
    NodeResult,
    NodeStatus,
    ResultReason,
)


def test_failed_result_requires_reason_and_category() -> None:
    with pytest.raises(ContractValidationError):
        NodeResult("N", "a", NodeStatus.FAILED)


def test_succeeded_result_cannot_carry_failure_data() -> None:
    with pytest.raises(ContractValidationError):
        NodeResult(
            "N",
            "a",
            NodeStatus.SUCCEEDED,
            failure_category=FailureCategory.INTERNAL,
        )
    with pytest.raises(ContractValidationError):
        NodeResult("N", "a", NodeStatus.SUCCEEDED, reason=ResultReason("x", "failure"))


def test_checkpoint_required_needs_typed_request() -> None:
    with pytest.raises(ContractValidationError):
        NodeResult("N", "a", NodeStatus.CHECKPOINT_REQUIRED)

    result = NodeResult(
        "N",
        "a",
        NodeStatus.CHECKPOINT_REQUIRED,
        checkpoint_request=CheckpointRequest("approval", "Approval required"),
    )
    assert result.status is NodeStatus.CHECKPOINT_REQUIRED


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ContractValidationError):
        NodeResult("N", "a", "success")  # type: ignore[arg-type]
