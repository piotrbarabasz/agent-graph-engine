import threading
from dataclasses import replace

import pytest

from agentgraph.core import GraphState, RiskLevel, RunStatus
from agentgraph.core.state import RiskState
from agentgraph.runtime.codec import canonical_json_bytes, parse_json_bytes
from agentgraph.runtime.errors import (
    StateConflictError,
    StateCorruptionError,
    UnsupportedSchemaError,
)
from agentgraph.runtime.state_store import StateStore


def test_state_store_roundtrip_and_cas(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    state = GraphState.initial("r")
    persisted = store.initialize(state)
    next_state = replace(state, state_version=1, risk=RiskState(RiskLevel.LOW))
    updated = store.compare_and_swap(0, next_state)
    assert persisted.digest != updated.digest
    assert store.load() == next_state


@pytest.mark.parametrize("expected", [-1, 1])
def test_state_store_rejects_stale_and_premature_cas(tmp_path, expected: int) -> None:
    store = StateStore(tmp_path / "state.json")
    state = GraphState.initial("r")
    store.initialize(state)
    with pytest.raises(StateConflictError):
        store.compare_and_swap(expected, replace(state, state_version=expected + 1))


def test_state_store_detects_digest_and_json_corruption(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.initialize(GraphState.initial("r"))
    envelope = parse_json_bytes(path.read_bytes())
    envelope["state"]["run"]["status"] = RunStatus.FAILED.value
    path.write_bytes(canonical_json_bytes(envelope))
    with pytest.raises(StateCorruptionError, match="digest"):
        store.load()
    path.write_bytes(b"not json")
    with pytest.raises(StateCorruptionError):
        store.load()


def test_state_store_rejects_unknown_store_and_graph_schema(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.initialize(GraphState.initial("r"))
    envelope = parse_json_bytes(path.read_bytes())
    envelope["store_schema_version"] = 2
    path.write_bytes(canonical_json_bytes(envelope))
    with pytest.raises(UnsupportedSchemaError):
        store.load()


@pytest.mark.parametrize("mutation", ["graph_schema", "invalid_enum", "unknown_field"])
def test_state_contract_corruption_fails_closed(tmp_path, mutation: str) -> None:
    from agentgraph.runtime.codec import sha256_digest

    path = tmp_path / "state.json"
    store = StateStore(path)
    store.initialize(GraphState.initial("r"))
    envelope = parse_json_bytes(path.read_bytes())
    if mutation == "graph_schema":
        envelope["state"]["schema_version"] = 2
    elif mutation == "invalid_enum":
        envelope["state"]["run"]["status"] = "invalid"
    else:
        envelope["state"]["unknown"] = True
    envelope["state_digest"] = sha256_digest(envelope["state"])
    path.write_bytes(canonical_json_bytes(envelope))
    with pytest.raises(StateCorruptionError):
        store.load()


def test_concurrent_stale_state_writer_cannot_overwrite(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    state = GraphState.initial("r")
    store.initialize(state)
    outcomes: list[str] = []

    def write(level: RiskLevel) -> None:
        try:
            store.compare_and_swap(
                0,
                replace(state, state_version=1, risk=RiskState(level)),
            )
            outcomes.append("written")
        except StateConflictError:
            outcomes.append("conflict")

    threads = [
        threading.Thread(target=write, args=(level,)) for level in (RiskLevel.LOW, RiskLevel.HIGH)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "written"]
