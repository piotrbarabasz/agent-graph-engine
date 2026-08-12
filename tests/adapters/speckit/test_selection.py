from __future__ import annotations

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.work import SelectionKind
from tests.adapters.speckit.conftest import write_compatibility_source


def test_manifest_item_order_not_identifier_or_document_order_drives_selection(
    speckit_source,
) -> None:
    root, adapter = speckit_source
    manifest = root / ".specify" / "workstreams" / "E007.yml"
    content = manifest.read_text(encoding="utf-8")
    content = content.replace("  - T048\n  - T049\n  - T050", "  - T050\n  - T049\n  - T048")
    manifest.write_text(content, encoding="utf-8")
    tasks = root / "specs" / "001-ai-content-studio" / "tasks.md"
    content = tasks.read_text(encoding="utf-8")
    content = content.replace("**Dependencies:** `T048`", "**Dependencies:** None")
    content = content.replace("**Dependencies:** T049", "**Dependencies:** None")
    tasks.write_text(content, encoding="utf-8")

    selection = adapter.next_ready_item(adapter.snapshot(), "E007")

    assert selection.kind is SelectionKind.READY
    assert selection.item_id == "T050"


def test_complete_and_empty_scope_are_distinct(speckit_source) -> None:
    root, adapter = speckit_source
    tasks = root / "specs" / "001-ai-content-studio" / "tasks.md"
    tasks.write_text(tasks.read_text(encoding="utf-8").replace("- [ ]", "- [X]"), encoding="utf-8")
    complete = adapter.next_ready_item(adapter.snapshot(), "E007")
    assert complete.kind is SelectionKind.SCOPE_COMPLETE

    manifest = root / ".specify" / "workstreams" / "E007.yml"
    content = manifest.read_text(encoding="utf-8")
    content = content.replace("tasks:\n  - T048\n  - T049\n  - T050", "tasks: []")
    manifest.write_text(content, encoding="utf-8")
    tasks.write_text("# No declared work\n", encoding="utf-8")
    empty = adapter.next_ready_item(adapter.snapshot(), "E007")
    assert empty.kind is SelectionKind.EMPTY_SCOPE


def test_cross_scope_pending_dependency_produces_blocked_result(tmp_path) -> None:
    root = tmp_path / "target"
    root.mkdir()
    write_compatibility_source(root, multi_scope=True)
    adapter = SpecKitAdapter(SpecKitLayout(root))

    selection = adapter.next_ready_item(adapter.snapshot(), "E008")

    assert selection.kind is SelectionKind.BLOCKED_DEPENDENCIES
    assert selection.blocking_item_ids == ("T050",)


def test_parent_declaration_order_drives_scope_selection(tmp_path) -> None:
    root = tmp_path / "target"
    root.mkdir()
    write_compatibility_source(root, multi_scope=True)
    parent = root / ".specify" / "workstreams" / "M004.yml"
    parent.write_text(
        parent.read_text(encoding="utf-8").replace("  - E007\n  - E008", "  - E008\n  - E007"),
        encoding="utf-8",
    )
    second = root / ".specify" / "workstreams" / "E008.yml"
    second.write_text(
        second.read_text(encoding="utf-8").replace("depends_on:\n  - E007", "depends_on: []"),
        encoding="utf-8",
    )
    tasks = root / "specs" / "002-narration" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace(
            "**Dependencies:** T050", "**Dependencies:** None"
        ),
        encoding="utf-8",
    )
    adapter = SpecKitAdapter(SpecKitLayout(root))

    selection = adapter.next_ready_scope(adapter.snapshot(), "M004")

    assert selection.kind is SelectionKind.READY
    assert selection.scope_id == "E008"
