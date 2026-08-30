from __future__ import annotations

import json
import os

import pytest

from agentgraph.infra import ProcessRunner
from agentgraph.providers.codex import (
    CodexCliCapabilities,
    CodexCliProbe,
    CodexInvocationRuntime,
    CodexProviderConfig,
    CodexResponseError,
    restricted_permission_config_overrides,
)


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


def test_windows_policy_adds_elevated_backend_without_weakening_profile() -> None:
    windows = restricted_permission_config_overrides(windows=True)
    non_windows = restricted_permission_config_overrides(windows=False)

    assert windows[:-1] == non_windows
    assert windows[-1] == 'windows.sandbox="elevated"'
    combined = "\n".join(windows)
    assert '":root" = "deny"' in combined
    assert "network = { enabled = false }" in combined
    assert "danger-full-access" not in combined


def test_probe_and_runtime_share_the_restrictive_permission_overrides(tmp_path) -> None:
    config = CodexProviderConfig(executable="fixture-codex")
    probe = CodexCliProbe(ProcessRunner(), config)
    runtime = CodexInvocationRuntime(ProcessRunner(), config)
    capabilities = CodexCliCapabilities(
        version="fixture",
        supports_exec=True,
        supports_noninteractive_no_approval=True,
        supports_runtime_config_overrides=True,
        supports_strict_config=True,
        supports_structured_output=True,
        supports_final_output_file=True,
        supports_model_override=True,
        supports_isolated_configuration=True,
        supports_pinned_cwd=True,
        supports_restricted_filesystem_permissions=True,
    )
    probe_argv = probe._restricted_profile_argv(tmp_path)
    runtime_argv = runtime.invocation(
        tmp_path, tmp_path / "schema.json", tmp_path / "result.json", capabilities
    )

    expected = restricted_permission_config_overrides()
    assert all(_has_config(probe_argv, value) for value in expected)
    assert all(_has_config(runtime_argv, value) for value in expected)


def _has_config(argv: tuple[str, ...], expected: str) -> bool:
    return any(argv[index : index + 2] == ("--config", expected) for index in range(len(argv) - 1))


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
