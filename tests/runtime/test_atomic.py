import pytest

from agentgraph.runtime.atomic import atomic_write_bytes


def test_atomic_replace_removes_temp_and_replaces_content(tmp_path) -> None:
    target = tmp_path / "state.json"
    atomic_write_bytes(target, b"old")
    atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert list(tmp_path.glob(".state.json.*")) == []


@pytest.mark.parametrize("stage", ["before", "replace"])
def test_atomic_failure_preserves_previous_file(tmp_path, stage: str) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    def fail_before(temp, destination):
        del temp, destination
        raise OSError("injected before replace")

    def fail_replace(source, destination):
        del source, destination
        raise OSError("injected replace failure")

    kwargs = {"before_replace": fail_before} if stage == "before" else {"replace": fail_replace}
    with pytest.raises(OSError, match="injected"):
        atomic_write_bytes(target, b"new", **kwargs)
    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".state.json.*")) == []
