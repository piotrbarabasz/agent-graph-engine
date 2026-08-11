from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.runtime.codec import canonical_json_bytes, format_timestamp
from agentgraph.runtime.errors import (
    ActiveRunExistsError,
    InvalidRuntimeIdentifierError,
    StaleLeaseMismatchError,
)
from agentgraph.runtime.locking import ProjectLock
from tests.runtime.test_coordinator import coordinator
from tests.runtime.test_paths_ids import MALICIOUS_RUN_IDS


def _tree(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    entries = []
    for path in root.rglob("*"):
        entries.append(
            (
                path.relative_to(root).as_posix(),
                "directory" if path.is_dir() else "file",
                None if path.is_dir() else path.read_bytes(),
            )
        )
    return tuple(sorted(entries))


@pytest.mark.parametrize("run_id", MALICIOUS_RUN_IDS)
@pytest.mark.parametrize(
    "operation",
    ["start_run", "recover_incomplete_run_initialization", "open_session"],
)
def test_coordinator_rejects_unsafe_run_id_before_filesystem_mutation(
    runtime_paths, project, run_id: str, operation: str
) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    workspace = runtime_paths.root.parent
    before = _tree(workspace)

    with pytest.raises(InvalidRuntimeIdentifierError):
        getattr(runtime, operation)(run_id)

    assert _tree(workspace) == before


@pytest.mark.parametrize(
    ("persisted_project_id", "persisted_run_id"),
    [
        ("prj_../../outside", "run_safe"),
        ("prj_safe", "run_../../outside"),
    ],
)
def test_malicious_persisted_active_run_identity_fails_closed(
    runtime_paths, project, fixed_now, persisted_project_id: str, persisted_run_id: str
) -> None:
    runtime, _ = coordinator(runtime_paths, project)
    active_path = runtime_paths.active_run(project.project_id)
    active_path.write_bytes(
        canonical_json_bytes(
            {
                "project_id": persisted_project_id,
                "run_id": persisted_run_id,
                "created_at": format_timestamp(fixed_now()),
                "schema_version": 1,
            }
        )
    )
    before = _tree(runtime_paths.root.parent)

    with pytest.raises(ActiveRunExistsError, match="corrupt"):
        runtime._read_active_run()

    assert _tree(runtime_paths.root.parent) == before


@pytest.mark.parametrize(
    ("persisted_project_id", "persisted_run_id"),
    [
        ("prj_../../outside", "run_safe"),
        ("prj_safe", "run_../../outside"),
    ],
)
def test_malicious_persisted_lock_identity_writes_no_recovery_evidence(
    tmp_path, fixed_now, persisted_project_id: str, persisted_run_id: str
) -> None:
    timestamp = format_timestamp(fixed_now())
    metadata_path = tmp_path / "runtime" / "projects" / "prj_safe" / "lock.json"
    metadata_path.parent.mkdir(parents=True)
    raw = canonical_json_bytes(
        {
            "project_id": persisted_project_id,
            "run_id": persisted_run_id,
            "pid": 1,
            "hostname": "host",
            "acquired_at": timestamp,
            "heartbeat_at": timestamp,
            "engine_version": "0.1.0",
            "schema_version": 1,
        }
    )
    metadata_path.write_bytes(raw)
    evidence = metadata_path.parent / "recovery"
    lock = ProjectLock(
        metadata_path.parent / "project.lock",
        metadata_path,
        project_id="prj_safe",
        run_id="run_safe",
        recovery=True,
        recovery_evidence_dir=evidence,
    )

    with pytest.raises(StaleLeaseMismatchError):
        lock.acquire()

    assert metadata_path.read_bytes() == raw
    assert not evidence.exists()
