import threading

import pytest

from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.runtime.errors import ProjectRegistryError


def test_same_root_is_stable_different_roots_are_distinct_and_reload(tmp_path, fixed_now) -> None:
    paths = RuntimePaths.resolve(tmp_path / "runtime")
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        root.mkdir()
    ids = iter(["prj_00000000000000000000000001", "prj_00000000000000000000000002"])
    registry = ProjectRegistry(paths, project_id_factory=lambda: next(ids), now=fixed_now)
    first = registry.register(roots[0])
    assert registry.register(roots[0]) == first
    second = registry.register(roots[1])
    assert first.project_id != second.project_id
    assert ProjectRegistry(paths).get(first.project_id) == first


def test_registry_corruption_and_project_disagreement_fail_closed(runtime_paths, project) -> None:
    runtime_paths.registry.write_text("broken", encoding="utf-8")
    with pytest.raises(ProjectRegistryError):
        ProjectRegistry(runtime_paths).get(project.project_id)


def test_project_file_disagreement_is_detected(runtime_paths, project) -> None:
    runtime_paths.project_record(project.project_id).write_text("{}", encoding="utf-8")
    with pytest.raises(ProjectRegistryError, match=r"project\.json"):
        ProjectRegistry(runtime_paths).get(project.project_id)


def test_registration_never_modifies_target_repository(runtime_paths, tmp_path, fixed_now) -> None:
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    before = {
        path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
    }
    ProjectRegistry(
        runtime_paths,
        project_id_factory=lambda: "prj_00000000000000000000000003",
        now=fixed_now,
    ).register(target)
    after = {
        path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()
    }
    assert after == before


def test_concurrent_registration_of_same_root_returns_one_identity(
    runtime_paths, tmp_path, fixed_now
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    counter = iter(range(10))
    registry = ProjectRegistry(
        runtime_paths,
        project_id_factory=lambda: f"prj_{next(counter):026d}",
        now=fixed_now,
    )
    results = []

    def register() -> None:
        results.append(registry.register(target).project_id)

    threads = [threading.Thread(target=register) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1
