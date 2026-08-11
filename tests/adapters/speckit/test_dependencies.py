from __future__ import annotations

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from tests.adapters.speckit.conftest import write_compatibility_source


def codes(adapter) -> set[str]:
    return {issue.code for issue in adapter.validate().issues}


def multi_source(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    write_compatibility_source(root, multi_scope=True)
    return root, SpecKitAdapter(SpecKitLayout(root))


def test_declared_cross_scope_item_dependency_is_valid(tmp_path) -> None:
    _, adapter = multi_source(tmp_path)

    assert adapter.validate().ok


def test_unauthorized_cross_scope_item_dependency_is_rejected(tmp_path) -> None:
    root, adapter = multi_source(tmp_path)
    manifest = root / ".specify" / "workstreams" / "E008.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("depends_on:\n  - E007", "depends_on: []"),
        encoding="utf-8",
    )

    assert "cross_scope_dependency_not_declared" in codes(adapter)


def test_unknown_scope_dependency_and_scope_cycle_are_rejected(tmp_path) -> None:
    root, adapter = multi_source(tmp_path)
    first = root / ".specify" / "workstreams" / "E007.yml"
    original = first.read_text(encoding="utf-8")
    first.write_text(original.replace("depends_on: []", "depends_on:\n  - E999"), encoding="utf-8")
    assert "unknown_scope_dependency" in codes(adapter)

    first.write_text(original.replace("depends_on: []", "depends_on:\n  - E008"), encoding="utf-8")
    assert "scope_dependency_cycle" in codes(adapter)


def test_parent_child_bidirectional_consistency_is_enforced(tmp_path) -> None:
    root, adapter = multi_source(tmp_path)
    parent = root / ".specify" / "workstreams" / "M004.yml"
    parent.write_text(
        parent.read_text(encoding="utf-8").replace("  - E008\n", ""), encoding="utf-8"
    )

    assert "child_omitted_by_parent" in codes(adapter)


def test_parent_listing_unknown_child_is_rejected(tmp_path) -> None:
    root, adapter = multi_source(tmp_path)
    parent = root / ".specify" / "workstreams" / "M004.yml"
    parent.write_text(
        parent.read_text(encoding="utf-8").replace("  - E008\n", "  - E999\n"),
        encoding="utf-8",
    )

    assert "parent_lists_unknown_child" in codes(adapter)


def test_item_declared_by_multiple_scope_manifests_is_rejected(tmp_path) -> None:
    root, adapter = multi_source(tmp_path)
    second = root / ".specify" / "workstreams" / "E008.yml"
    second.write_text(
        second.read_text(encoding="utf-8").replace("  - T051", "  - T050"),
        encoding="utf-8",
    )

    assert "item_owned_by_multiple_scopes" in codes(adapter)
