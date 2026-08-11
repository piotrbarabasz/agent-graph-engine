import threading
from pathlib import Path

import pytest

from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.runtime.codec import canonical_json_bytes, format_timestamp
from agentgraph.runtime.errors import (
    InvalidRuntimeIdentifierError,
    ProjectRegistryError,
    RuntimePathError,
)


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


@pytest.mark.parametrize("runtime_location", ["same", "descendant"])
def test_registration_rejects_runtime_root_inside_target_without_modifying_target(
    tmp_path, fixed_now, runtime_location: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("unchanged", encoding="utf-8")
    runtime_root = target if runtime_location == "same" else target / ".agentgraph"
    before = _tree(target)

    with pytest.raises(RuntimePathError, match="outside"):
        ProjectRegistry(RuntimePaths.resolve(runtime_root), now=fixed_now).register(target)

    assert _tree(target) == before
    if runtime_location == "descendant":
        assert not runtime_root.exists()


def test_registry_with_malicious_persisted_project_id_fails_closed(tmp_path, fixed_now) -> None:
    paths = RuntimePaths.resolve(tmp_path / "runtime")
    paths.root.mkdir(parents=True)
    timestamp = format_timestamp(fixed_now())
    paths.registry.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "projects": [
                    {
                        "project_id": "prj_../../outside",
                        "canonical_root": str((tmp_path / "target").resolve()),
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "schema_version": 1,
                    }
                ],
            }
        )
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("unchanged", encoding="utf-8")
    before = _tree(tmp_path)

    with pytest.raises(ProjectRegistryError, match="corrupted"):
        ProjectRegistry(paths).get("prj_safe")

    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    "project_id",
    [
        "prj_../../outside",
        r"prj_..\..\outside",
        "prj_/absolute",
        r"prj_\absolute",
        "prj_:evil",
        "prj_ bad",
    ],
)
def test_registration_rejects_unsafe_generated_project_id_without_touching_target(
    tmp_path, fixed_now, project_id: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("unchanged", encoding="utf-8")
    before = _tree(target)
    paths = RuntimePaths.resolve(tmp_path / "runtime")

    with pytest.raises(InvalidRuntimeIdentifierError):
        ProjectRegistry(
            paths,
            project_id_factory=lambda: project_id,
            now=fixed_now,
        ).register(target)

    assert _tree(target) == before
    assert not (tmp_path / "outside").exists()


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
