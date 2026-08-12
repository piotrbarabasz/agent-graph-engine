from __future__ import annotations

import pytest

from agentgraph.work import InvalidWorkSourceError


def write_active(root, value: str) -> None:
    path = root / ".specify" / "runtime" / "active-epic"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_active_scope_file_is_optional_read_only_hint(speckit_source) -> None:
    root, adapter = speckit_source
    assert adapter.snapshot().active_scope_id is None

    write_active(root, "E007\n")

    assert adapter.snapshot().active_scope_id == "E007"
    assert adapter.next_ready_item(adapter.snapshot(), "E007").scope_id == "E007"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "active_scope_empty"),
        ("bad", "active_scope_malformed"),
        ("E999", "active_scope_unknown"),
    ],
)
def test_invalid_active_scope_fails_snapshot(speckit_source, value: str, code: str) -> None:
    root, adapter = speckit_source
    write_active(root, value)

    assert code in {issue.code for issue in adapter.validate().issues}
    with pytest.raises(InvalidWorkSourceError):
        adapter.snapshot()
