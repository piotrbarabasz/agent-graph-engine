from __future__ import annotations

import json

import pytest

from agentgraph.providers.codex import CodexProposalStatus, CodexResponseError, parse_codex_proposal


def _raw(**overrides) -> bytes:
    value = {
        "schema_version": 1,
        "status": "changes",
        "changes": [{"path": "src/a.py", "content": "a\n"}],
        "reason_code": None,
        "message": None,
    }
    value.update(overrides)
    return json.dumps(value).encode()


def test_parser_accepts_exact_changes_and_blocked_contracts() -> None:
    changes = parse_codex_proposal(_raw())
    blocked = parse_codex_proposal(
        _raw(status="blocked", changes=[], reason_code="requires_delete", message="Needs delete")
    )

    assert changes.status is CodexProposalStatus.CHANGES
    assert blocked.status is CodexProposalStatus.BLOCKED
    assert changes.digest.startswith("sha256:")


@pytest.mark.parametrize(
    "raw",
    (
        b"{not-json",
        b'Sure! {"schema_version":1}',
        _raw(extra="forbidden"),
        _raw(status="unknown"),
        _raw(changes=[]),
        _raw(
            changes=[
                {"path": "src/a.py", "content": "a"},
                {"path": "src/a.py", "content": "b"},
            ]
        ),
        b'{"schema_version":1,"schema_version":1,"status":"changes","changes":[],"reason_code":null,"message":null}',
    ),
)
def test_parser_rejects_malformed_freeform_extra_duplicate_and_invalid_status(raw) -> None:
    with pytest.raises(CodexResponseError):
        parse_codex_proposal(raw)
