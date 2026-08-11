from __future__ import annotations

import pytest

from agentgraph.runtime.codec import decode_value, parse_json_bytes
from agentgraph.runtime.errors import ActiveRunExistsError
from agentgraph.runtime.lifecycle import ActiveRunRecord
from tests.runtime.test_coordinator import coordinator


def active_record(runtime_paths, project):
    path = runtime_paths.active_run(project.project_id)
    if not path.exists():
        return None
    return decode_value(parse_json_bytes(path.read_bytes()), ActiveRunRecord)


def test_one_unfinished_writer_run_owns_project_across_sessions(runtime_paths, project) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    first = runtime.start_run("run_A")
    assert active_record(runtime_paths, project).run_id == first.run_id

    with pytest.raises(ActiveRunExistsError):
        runtime.start_run("run_B")

    with runtime.open_session(first.run_id) as session:
        session.step()
    with pytest.raises(ActiveRunExistsError):
        runtime.start_run("run_B")
    assert active_record(runtime_paths, project).run_id == first.run_id


def test_terminal_run_releases_ownership_and_allows_next_run(runtime_paths, project) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    first = runtime.start_run("run_A")
    with runtime.open_session(first.run_id) as session:
        while session.store.load().graph.current_node != "END":
            session.step()
    assert active_record(runtime_paths, project) is None

    second = runtime.start_run("run_B")
    assert active_record(runtime_paths, project).run_id == second.run_id


def test_repeated_or_stale_finalization_cannot_clear_another_runs_ownership(
    runtime_paths, project
) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    first = runtime.start_run("run_A")
    with runtime.open_session(first.run_id) as session:
        while session.store.load().graph.current_node != "END":
            session.step()
    second = runtime.start_run("run_B")

    with runtime.open_session(first.run_id) as stale_session:
        persisted = stale_session.store.load_persisted()
        stale_session._finalize(persisted.state, persisted.digest)

    assert active_record(runtime_paths, project).run_id == second.run_id


def test_terminal_active_record_is_verified_and_cleaned_before_new_start(
    runtime_paths, project
) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    first = runtime.start_run("run_A")
    with runtime.open_session(first.run_id) as session:
        while session.store.load().graph.current_node != "END":
            session.step()
    runtime._write_active_run(first.run_id)

    second = runtime.start_run("run_B")

    assert active_record(runtime_paths, project).run_id == second.run_id
