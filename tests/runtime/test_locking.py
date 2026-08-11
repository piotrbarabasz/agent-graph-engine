import pytest

from agentgraph.runtime.codec import canonical_json_bytes, format_timestamp, utc_now
from agentgraph.runtime.errors import (
    ProjectLockedError,
    StaleLeaseError,
    StaleLeaseMismatchError,
)
from agentgraph.runtime.locking import AdvisoryFileLock, LockMetadata, ProjectLock


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
    timestamp = format_timestamp(utc_now())
    lease.write_bytes(
        canonical_json_bytes(
            LockMetadata(
                "prj_00000000000000000000000000",
                "run_a",
                1,
                "host",
                timestamp,
                timestamp,
            )
        )
    )
    with pytest.raises(StaleLeaseError):
        make_lock(tmp_path).acquire()
    with make_lock(tmp_path, recovery=True) as recovered:
        assert recovered.metadata is not None
        assert list((tmp_path / "recovery").glob("stale-lease-*.json"))


def write_stale_lease(tmp_path, *, project_id: str, run_id: str) -> bytes:
    timestamp = format_timestamp(utc_now())
    raw = canonical_json_bytes(LockMetadata(project_id, run_id, 1, "host", timestamp, timestamp))
    (tmp_path / "lock.json").write_bytes(raw)
    return raw


@pytest.mark.parametrize(
    ("requested_project", "requested_run"),
    [
        ("prj_00000000000000000000000000", "run_B"),
        ("prj_00000000000000000000000001", "run_A"),
    ],
)
def test_stale_lease_recovery_rejects_identity_mismatch_without_overwrite(
    tmp_path, requested_project: str, requested_run: str
) -> None:
    raw = write_stale_lease(
        tmp_path,
        project_id="prj_00000000000000000000000000",
        run_id="run_A",
    )
    lock = ProjectLock(
        tmp_path / "project.lock",
        tmp_path / "lock.json",
        project_id=requested_project,
        run_id=requested_run,
        recovery=True,
    )
    with pytest.raises(StaleLeaseMismatchError):
        lock.acquire()
    assert (tmp_path / "lock.json").read_bytes() == raw
    assert not (tmp_path / "recovery").exists()


def test_metadata_cleanup_failure_still_releases_os_lock(tmp_path, monkeypatch) -> None:
    lock = make_lock(tmp_path)
    lock.acquire()

    def fail_cleanup() -> None:
        raise OSError("unlink failed")

    monkeypatch.setattr(lock, "_remove_metadata", fail_cleanup)
    with pytest.raises(OSError, match="unlink"):
        lock.release()

    proof = AdvisoryFileLock(tmp_path / "project.lock")
    proof.acquire()
    proof.release()
