from dataclasses import FrozenInstanceError

import pytest

from agentgraph.core import GraphState, WorkItem
from agentgraph.core.errors import ContractValidationError
from agentgraph.core.state import WorkState


def test_initial_state_is_versioned_and_immutable() -> None:
    state = GraphState.initial("run-1", max_repair_cycles=2)

    assert state.schema_version == 1
    assert state.state_version == 0
    assert state.graph.current_node == "START"
    assert state.repair.count == 0
    assert state.repair.max_cycles == 2
    with pytest.raises(FrozenInstanceError):
        state.state_version = 9  # type: ignore[misc]


def test_neutral_work_item_requires_no_source_hierarchy_concepts() -> None:
    item = WorkItem(id="ABC-123", title="Neutral unit")
    state = GraphState(run=GraphState.initial("r").run, work=WorkState(item=item))

    assert state.work.item == item
    assert state.work.hierarchy == ()


@pytest.mark.parametrize("version", [0, 2, -1])
def test_invalid_schema_or_state_versions_are_rejected(version: int) -> None:
    kwargs = {"schema_version": version} if version != -1 else {"state_version": version}
    with pytest.raises(ContractValidationError):
        GraphState(run=GraphState.initial("r").run, **kwargs)
