from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError

import pytest

from agentgraph.infra import CommandSpec, ProcessRunner
from agentgraph.infra.receipts import generate_command_id, validate_command_id


def test_command_receipt_is_immutable_versioned_and_json_serializable(tmp_path) -> None:
    result = ProcessRunner(command_id_factory=lambda: "cmd_deterministic").run(
        CommandSpec((sys.executable, "-c", "print('ok')"), tmp_path)
    )
    receipt = result.receipt

    assert receipt.schema_version == 1
    assert receipt.command_id == "cmd_deterministic"
    assert receipt.started_at.endswith("Z")
    assert receipt.finished_at.endswith("Z")
    assert receipt.duration_ms >= 0
    json.dumps(receipt.to_dict())
    with pytest.raises(FrozenInstanceError):
        receipt.duration_ms = 0


def test_generated_command_ids_pass_canonical_validator() -> None:
    for _ in range(20):
        assert validate_command_id(generate_command_id()).startswith("cmd_")
