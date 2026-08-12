from __future__ import annotations

import json
import os

import pytest

from agentgraph.providers.codex import CodexResponseError


def test_codex_child_does_not_inherit_ambient_secrets(codex_fixture, monkeypatch) -> None:
    for key in (
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "SOME_TEST_SECRET",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(key, f"secret-{key}")
    monkeypatch.setenv(
        "FAKE_CODEX_RESULT",
        '{"schema_version":1,"status":"changes","changes":[{"path":"src/new.py",'
        '"content":"safe\\n"}],"reason_code":null,"message":null}',
    )

    codex_fixture["provider"].propose(codex_fixture["request"], codex_fixture["context"])

    capture = json.loads(codex_fixture["capture"].read_text(encoding="utf-8"))
    assert not any(capture["sensitive_visible"].values())


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink API unavailable")
def test_result_file_symlink_is_rejected(codex_fixture, monkeypatch) -> None:
    probe_target = codex_fixture["context"].runtime_directory / "probe-target"
    probe_link = codex_fixture["context"].runtime_directory / "probe-link"
    probe_target.write_text("test", encoding="utf-8")
    try:
        probe_link.symlink_to(probe_target)
    except OSError:
        pytest.skip("symlink creation unavailable to current user")
    probe_link.unlink()
    probe_target.unlink()
    monkeypatch.setenv("FAKE_CODEX_MODE", "symlink")

    with pytest.raises(CodexResponseError):
        codex_fixture["provider"].propose(codex_fixture["request"], codex_fixture["context"])
