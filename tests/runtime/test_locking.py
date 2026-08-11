import pytest

from agentgraph.runtime.errors import ProjectLockedError, StaleLeaseError
from agentgraph.runtime.locking import ProjectLock


def make_lock(tmp_path, run_id="run_a", recovery=False):
    return ProjectLock(
        tmp_path / "project.lock",
        tmp_path / "lock.json",
        project_id="prj_00000000000000000000000000",
        run_id=run_id,
        recovery=recovery,
    )


def test_exclusive_project_lock_release_and_metadata(tmp_path) -> None:
    first = make_lock(tmp_path)
    first.acquire()
    assert first.metadata_path.exists()
    assert first.read_metadata()["run_id"] == "run_a"
    with pytest.raises(ProjectLockedError):
        make_lock(tmp_path, "run_b").acquire()
    first.release()
    assert not first.metadata_path.exists()
    with make_lock(tmp_path, "run_b"):
        pass


def test_stale_metadata_requires_explicit_recovery(tmp_path) -> None:
    lease = tmp_path / "lock.json"
    lease.write_text("{}", encoding="utf-8")
    with pytest.raises(StaleLeaseError):
        make_lock(tmp_path).acquire()
    with make_lock(tmp_path, recovery=True) as recovered:
        assert recovered.metadata is not None
        assert list((tmp_path / "recovery").glob("stale-lease-*.json"))
