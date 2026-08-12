from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from agentgraph.infra import CancellationToken
from agentgraph.providers.codex import (
    CodexChangeProvider,
    CodexInvocationError,
    CodexProposalError,
    CodexProviderBlockedError,
    CodexProviderConfig,
    CodexProviderContextError,
    CodexResponseError,
    CodexTimeoutError,
    build_codex_change_prompt,
    restricted_permission_config_overrides,
)
from agentgraph.write.evidence import read_evidence
from tests.providers.codex.conftest import proposal


def test_implementation_prompt_contains_bounded_analysis_but_preserves_authority(
    codex_fixture,
) -> None:
    request = replace(
        codex_fixture["request"],
        analysis_summary=("Existing service boundary is stable.",),
        implementation_plan=("Change only the declared service module.",),
        validation_focus=("Exercise the public behavior.",),
        derived_constraints=("Preserve the interface.",),
        relevant_files=("src/interface.py",),
        effective_requirements=("Source requirement.", "Derived requirement."),
        effective_acceptance_criteria=("Acceptance one", "Derived criterion."),
    )

    prompt = build_codex_change_prompt(request).decode("utf-8")

    for expected in (
        "Existing service boundary is stable.",
        "Change only the declared service module.",
        "Exercise the public behavior.",
        "Preserve the interface.",
        "src/interface.py",
        "src/existing.py",
        request.baseline_head,
        request.source_revision,
        "Acceptance one",
        "EFFECTIVE REQUIREMENTS",
        "EFFECTIVE ACCEPTANCE CRITERIA",
        "Source requirement.",
        "Derived requirement.",
        "Derived criterion.",
    ):
        assert expected in prompt


def test_provider_uses_stdin_restricted_profile_and_engine_computes_existing_hash(
    codex_fixture, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_RESULT", proposal("src/existing.py", "value = 2\n"))

    changeset = codex_fixture["provider"].propose(
        codex_fixture["request"], codex_fixture["context"]
    )

    expected = hashlib.sha256(
        (codex_fixture["repository"] / "src" / "existing.py").read_bytes()
    ).hexdigest()
    assert changeset.changes[0].expected_before_sha256 == expected
    capture = json.loads(codex_fixture["capture"].read_text(encoding="utf-8"))
    argv = capture["argv"]
    assert codex_fixture["request"].goal not in " ".join(argv)
    assert codex_fixture["request"].goal in capture["prompt"]
    assert argv[argv.index("--cd") :][:2] == [
        "--cd",
        str(codex_fixture["repository"].resolve()),
    ]
    assert capture["cwd"] == str(codex_fixture["repository"].resolve())
    assert "--sandbox" not in argv
    assert 'default_permissions="agentgraph_provider"' in argv
    profile = next(value for value in argv if value.startswith("permissions.agentgraph_provider="))
    assert '":root" = "deny"' in profile
    assert '":minimal" = "read"' in profile
    assert '":workspace_roots" = { "." = "read" }' in profile
    assert "network = { enabled = false }" in profile
    assert 'approval_policy="never"' in argv
    assert "mcp_servers={}" in argv
    assert 'web_search="disabled"' in argv
    assert "--output-schema" in argv and "--output-last-message" in argv
    for required_flag in (
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
    ):
        assert required_flag in argv
    assert argv[-1] == "-"
    invocation = "\n".join(argv)
    for forbidden in (
        "workspace-write",
        "danger-full-access",
        "--full-auto",
        '":root" = "read"',
    ):
        assert forbidden not in invocation
    codex_dir = codex_fixture["context"].runtime_directory / "codex"
    receipt = read_evidence(codex_dir / "codex-receipt.json")
    evidence = read_evidence(codex_dir / "codex-proposal.json")
    assert receipt["payload"]["prompt_digest"].startswith("sha256:")
    assert evidence["payload"]["proposal_digest"].startswith("sha256:")
    assert set(evidence["payload"]) == {"proposal", "proposal_digest"}


def test_restricted_permission_policy_grants_only_workspace_read(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (outside / "secret.txt").write_text("sentinel\n", encoding="utf-8")

    policy = tomllib.loads("\n".join(restricted_permission_config_overrides()))
    profile = policy["permissions"]["agentgraph_provider"]

    assert policy["default_permissions"] == "agentgraph_provider"
    assert profile["filesystem"] == {
        ":root": "deny",
        ":minimal": "read",
        ":workspace_roots": {".": "read"},
    }
    assert profile["network"] == {"enabled": False}
    assert str(outside.resolve()) not in "\n".join(restricted_permission_config_overrides())


@pytest.mark.parametrize(
    "arguments",
    (
        ("--cd", "outside"),
        ("-C", "outside"),
        ("--sandbox", "danger-full-access"),
        ("--config", 'sandbox_mode="danger-full-access"'),
    ),
)
def test_executable_arguments_reject_codex_option_escape(arguments) -> None:
    with pytest.raises(ValueError, match="non-option"):
        CodexProviderConfig(executable_arguments=arguments)


@pytest.mark.parametrize(
    ("path", "expected"),
    (("src/new.py", None), ("src/pkg/new.py", None)),
)
def test_provider_materializes_new_and_directory_capability_paths(
    codex_fixture, monkeypatch, path, expected
) -> None:
    monkeypatch.setenv("FAKE_CODEX_RESULT", proposal(path, "new = True\n"))

    changeset = codex_fixture["provider"].propose(
        codex_fixture["request"], codex_fixture["context"]
    )

    assert changeset.changes[0].expected_before_sha256 is expected


def test_provider_rejects_out_of_scope_proposal_before_engine_apply(
    codex_fixture, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_RESULT", proposal("README.md", "escape\n"))

    with pytest.raises(CodexProposalError) as raised:
        codex_fixture["provider"].propose(codex_fixture["request"], codex_fixture["context"])

    assert raised.value.code == "codex_proposal_out_of_scope"
    assert not (codex_fixture["repository"] / "README.md").exists()


def test_provider_maps_blocked_result_without_a_changeset(codex_fixture, monkeypatch) -> None:
    monkeypatch.setenv(
        "FAKE_CODEX_RESULT",
        json.dumps(
            {
                "schema_version": 1,
                "status": "blocked",
                "changes": [],
                "reason_code": "requires_delete",
                "message": "Task requires deleting a file.",
            }
        ),
    )

    with pytest.raises(CodexProviderBlockedError) as raised:
        codex_fixture["provider"].propose(codex_fixture["request"], codex_fixture["context"])

    assert raised.value.reason_code == "requires_delete"


@pytest.mark.parametrize(
    ("mode", "error"),
    (
        ("nonzero", CodexInvocationError),
        ("malformed", CodexResponseError),
        ("freeform", CodexResponseError),
        ("oversized", CodexResponseError),
    ),
)
def test_provider_fails_closed_on_invocation_and_output_errors(
    codex_fixture, monkeypatch, mode, error
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)

    with pytest.raises(error):
        codex_fixture["provider"].propose(codex_fixture["request"], codex_fixture["context"])


def test_provider_timeout_is_typed_and_reaped(codex_fixture, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "hang")
    original = codex_fixture["provider"]
    provider = CodexChangeProvider(
        process_runner=original.runner,
        git_adapter=original.git,
        config=replace(original.config, timeout_seconds=0.1),
    )

    with pytest.raises(CodexTimeoutError):
        provider.propose(codex_fixture["request"], codex_fixture["context"])


def test_provider_cancellation_is_a_single_failed_invocation(codex_fixture) -> None:
    original = codex_fixture["provider"]
    cancellation = CancellationToken()
    cancellation.cancel()
    provider = CodexChangeProvider(
        process_runner=original.runner,
        git_adapter=original.git,
        config=original.config,
        cancellation=cancellation,
    )

    with pytest.raises(CodexInvocationError):
        provider.propose(codex_fixture["request"], codex_fixture["context"])


def test_provider_rejects_repository_head_other_than_context_baseline(codex_fixture) -> None:
    context = replace(codex_fixture["context"], baseline_head="0" * 40)

    with pytest.raises(CodexProviderContextError):
        codex_fixture["provider"].propose(codex_fixture["request"], context)
