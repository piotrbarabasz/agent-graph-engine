from __future__ import annotations

import pytest


def codes(adapter) -> set[str]:
    return {issue.code for issue in adapter.validate().issues}


@pytest.mark.parametrize(
    ("needle", "replacement", "expected"),
    [
        ("  - **Epic:** E007\n", "", "missing_task_field"),
        ("  - **Milestone:** M004\n", "", "missing_task_field"),
        ("**Epic:** E007", "**Epic:** E999", "unknown_item_scope"),
        ("**Milestone:** M004", "**Milestone:** M999", "item_parent_mismatch"),
    ],
)
def test_task_owner_fields_are_required_and_consistent(
    speckit_source, needle, replacement, expected
) -> None:
    root, adapter = speckit_source
    path = root / "specs" / "001-ai-content-studio" / "tasks.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(needle, replacement, 1), encoding="utf-8"
    )

    assert expected in codes(adapter)


def test_item_omitted_from_manifest_is_rejected(speckit_source) -> None:
    root, adapter = speckit_source
    manifest = root / ".specify" / "workstreams" / "E007.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("  - T050\n", ""),
        encoding="utf-8",
    )

    assert "item_omitted_from_manifest" in codes(adapter)


def test_unknown_dependency_and_dependency_cycle_are_rejected(speckit_source) -> None:
    root, adapter = speckit_source
    tasks = root / "specs" / "001-ai-content-studio" / "tasks.md"
    original = tasks.read_text(encoding="utf-8")
    tasks.write_text(
        original.replace("**Dependencies:** T049", "**Dependencies:** T999"), encoding="utf-8"
    )
    assert "unknown_item_dependency" in codes(adapter)

    tasks.write_text(
        original.replace("**Dependencies:** `T048`", "**Dependencies:** T050"),
        encoding="utf-8",
    )
    assert "item_dependency_cycle" in codes(adapter)


def test_duplicate_task_id_in_document_is_rejected(speckit_source) -> None:
    root, adapter = speckit_source
    tasks = root / "specs" / "001-ai-content-studio" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace("T050 Add narration", "T049 Add narration"),
        encoding="utf-8",
    )

    assert "duplicate_item_id" in codes(adapter)
