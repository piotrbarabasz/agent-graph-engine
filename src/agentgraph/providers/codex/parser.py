"""Exact parsing for one Codex final result."""

from __future__ import annotations

import json
import re

from agentgraph.write.models import MAX_CHANGE_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES

from .errors import CodexResponseError
from .schema import CodexFileProposal, CodexProposal, CodexProposalStatus

_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z", re.ASCII)
_ROOT_FIELDS = {"schema_version", "status", "changes", "reason_code", "message"}
_CHANGE_FIELDS = {"path", "content"}


def parse_codex_proposal(raw: bytes) -> CodexProposal:
    try:
        text = raw.decode("utf-8")
        document = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CodexResponseError(
            "Codex final result is not one strict UTF-8 JSON document"
        ) from exc
    if not isinstance(document, dict) or set(document) != _ROOT_FIELDS:
        raise CodexResponseError("Codex final result has unknown or missing fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise CodexResponseError("unsupported Codex proposal schema version")
    try:
        status = CodexProposalStatus(document["status"])
    except (TypeError, ValueError) as exc:
        raise CodexResponseError("unknown Codex proposal status") from exc
    raw_changes = document["changes"]
    if not isinstance(raw_changes, list) or len(raw_changes) > MAX_CHANGE_FILES:
        raise CodexResponseError("Codex changes must be a bounded array")
    changes = []
    total_bytes = 0
    for value in raw_changes:
        if not isinstance(value, dict) or set(value) != _CHANGE_FIELDS:
            raise CodexResponseError("Codex file proposal has unknown or missing fields")
        path, content = value["path"], value["content"]
        if not isinstance(path, str) or not path or "\x00" in path:
            raise CodexResponseError("Codex proposal path is invalid")
        if not isinstance(content, str) or "\x00" in content:
            raise CodexResponseError("Codex proposal content must be NUL-free text")
        size = len(content.encode("utf-8"))
        total_bytes += size
        if size > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise CodexResponseError("Codex proposal content exceeds write limits")
        changes.append(CodexFileProposal(path, content))
    if len({change.path for change in changes}) != len(changes):
        raise CodexResponseError("Codex proposal contains duplicate paths")
    reason, message = document["reason_code"], document["message"]
    if status is CodexProposalStatus.CHANGES:
        if not changes or reason is not None or message is not None:
            raise CodexResponseError("changes proposal has invalid status fields")
    elif changes or not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        raise CodexResponseError("blocked proposal has invalid status fields")
    elif not isinstance(message, str) or len(message) > 2000:
        raise CodexResponseError("blocked proposal message is invalid")
    return CodexProposal(1, status, tuple(changes), reason, message)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result
