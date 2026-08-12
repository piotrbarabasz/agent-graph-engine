from __future__ import annotations

import pytest

from agentgraph.work import InvalidWorkSourceError


def issue_codes(adapter) -> set[str]:
    return {issue.code for issue in adapter.validate().issues}


def test_duplicate_yaml_keys_and_malformed_yaml_fail_closed(speckit_source) -> None:
    root, adapter = speckit_source
    manifest = root / ".specify" / "workstreams" / "E007.yml"
    manifest.write_text("id: E007\ntitle: one\ntitle: two\n", encoding="utf-8")

    assert "invalid_yaml" in issue_codes(adapter)
    with pytest.raises(InvalidWorkSourceError):
        adapter.snapshot()


def test_duplicate_manifest_ids_are_rejected(speckit_source) -> None:
    root, adapter = speckit_source
    source = root / ".specify" / "workstreams" / "E007.yml"
    (source.parent / "copy.yml").write_bytes(source.read_bytes())

    assert "duplicate_manifest_id" in issue_codes(adapter)


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("risk: high", "risk: unknown", "invalid_scope_risk"),
        ("status: planned", "status: unknown", "invalid_scope_status"),
        (
            "base_branch: master",
            "base_branch: epic/E007-tts-contract-fixtures",
            "branch_equals_base_branch",
        ),
        ("auto_merge: false", "auto_merge: true", "unsafe_source_policy"),
    ],
)
def test_scope_schema_safety_values_are_rejected(speckit_source, old, new, code) -> None:
    root, adapter = speckit_source
    path = root / ".specify" / "workstreams" / "E007.yml"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    assert code in issue_codes(adapter)


def test_validation_issue_order_is_deterministic(speckit_source) -> None:
    root, adapter = speckit_source
    path = root / ".specify" / "workstreams" / "E007.yml"
    content = path.read_text(encoding="utf-8")
    path.write_text(content.replace("risk: high", "risk: invalid"), encoding="utf-8")

    first = adapter.validate()
    second = adapter.validate()

    assert first == second
    assert tuple(issue.sort_key() for issue in first.issues) == tuple(
        sorted(issue.sort_key() for issue in first.issues)
    )


def test_active_scope_with_empty_or_duplicate_item_list_is_rejected(speckit_source) -> None:
    root, adapter = speckit_source
    path = root / ".specify" / "workstreams" / "E007.yml"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace("status: planned", "status: active").replace(
            "tasks:\n  - T048\n  - T049\n  - T050", "tasks: []"
        ),
        encoding="utf-8",
    )
    assert "active_scope_empty" in issue_codes(adapter)

    path.write_text(original.replace("  - T050", "  - T049"), encoding="utf-8")
    assert "duplicate_manifest_item" in issue_codes(adapter)
