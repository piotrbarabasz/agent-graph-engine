from pathlib import Path

from agentgraph.work import SelectionKind, ValidationOrigin, WorkItemStatus, WorkRisk, WorkSource


def test_compatibility_fixture_maps_to_neutral_snapshot_and_package(speckit_source) -> None:
    _, adapter = speckit_source
    assert isinstance(adapter, WorkSource)
    assert adapter.validate().ok

    snapshot = adapter.snapshot()
    selection = adapter.next_ready_item(snapshot, "E007")
    package = adapter.build_package(snapshot, "T049")

    assert selection.kind is SelectionKind.READY
    assert selection.item_id == "T049"
    assert adapter.get_item(snapshot, "T048").status is WorkItemStatus.COMPLETED
    assert package.title == "Add a provider-neutral result model"
    assert package.risk is WorkRisk.MEDIUM
    assert package.dependencies == ("T048",)
    assert package.source_location.path == "specs/001-ai-content-studio/tasks.md"
    assert package.source_location.line is not None
    assert package.source_revision is snapshot.revision
    assert len(package.item_validation_checks) == 2
    assert all(check.origin is ValidationOrigin.ITEM for check in package.item_validation_checks)
    assert len(package.scope_required_checks) == 2
    assert all(check.origin is ValidationOrigin.SCOPE for check in package.scope_required_checks)
    assert package.allowed_paths == (
        package.implementation_paths[0],
        package.implementation_paths[1],
        package.test_paths[0],
    )
    assert package.implementation_paths[1].directory_hint is True
    assert package.implementation_paths[0].path == "src/t049.py"
    assert not (Path(package.implementation_paths[0].path)).is_absolute()
