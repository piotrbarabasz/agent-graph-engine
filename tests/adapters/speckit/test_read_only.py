from __future__ import annotations

from pathlib import Path


def tree(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        sorted(
            (path.relative_to(root).as_posix(), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        )
    )


def test_all_public_operations_leave_entire_target_tree_byte_identical(speckit_source) -> None:
    root, adapter = speckit_source
    before = tree(root)

    validation = adapter.validate()
    snapshot = adapter.snapshot()
    adapter.get_scope(snapshot, "M004")
    adapter.get_scope(snapshot, "E007")
    adapter.get_item(snapshot, "T049")
    adapter.next_ready_item(snapshot, "E007")
    adapter.next_ready_scope(snapshot, "M004")
    adapter.build_package(snapshot, "T049")

    assert validation.ok
    assert tree(root) == before
    assert not (root / ".git").exists()
    assert not (root / ".agentgraph").exists()
