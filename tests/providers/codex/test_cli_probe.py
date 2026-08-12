from __future__ import annotations

from dataclasses import replace

import pytest

from agentgraph.providers.codex import (
    CodexChangeProvider,
    CodexCliUnavailableError,
    CodexCliUnsupportedError,
)


def test_probe_detects_all_required_fake_cli_capabilities(codex_fixture) -> None:
    capabilities = codex_fixture["provider"].capabilities(codex_fixture["repository"])

    assert capabilities.version == "codex-cli 9.9.9-fake"
    assert capabilities.required_supported is True
    assert capabilities.supports_model_override is True


def test_probe_fails_closed_when_isolation_flags_are_missing(codex_fixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_UNSUPPORTED", "1")

    with pytest.raises(CodexCliUnsupportedError):
        codex_fixture["provider"].capabilities(codex_fixture["repository"])


def test_probe_maps_missing_executable_without_install_attempt(codex_fixture) -> None:
    original = codex_fixture["provider"]
    provider = CodexChangeProvider(
        process_runner=original.runner,
        git_adapter=original.git,
        config=replace(
            original.config,
            executable="definitely-missing-codex-executable",
            executable_arguments=(),
        ),
    )

    with pytest.raises(CodexCliUnavailableError):
        provider.capabilities(codex_fixture["repository"])
