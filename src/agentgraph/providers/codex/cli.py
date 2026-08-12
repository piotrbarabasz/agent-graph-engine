"""Bounded capability inspection for the local Codex CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agentgraph.infra import CommandSpec, ProcessRunner, ProcessStatus
from agentgraph.infra.errors import ProcessStartError
from agentgraph.infra.redaction import is_sensitive_environment_key

from .config import CodexProviderConfig
from .errors import CodexCliUnavailableError, CodexCliUnsupportedError
from .policy import CODEX_PERMISSION_PROFILE_NAME, restricted_permission_config_overrides


@dataclass(frozen=True, slots=True)
class CodexCliCapabilities:
    version: str
    supports_exec: bool
    supports_noninteractive_no_approval: bool
    supports_runtime_config_overrides: bool
    supports_strict_config: bool
    supports_structured_output: bool
    supports_final_output_file: bool
    supports_model_override: bool
    supports_isolated_configuration: bool
    supports_pinned_cwd: bool
    supports_restricted_filesystem_permissions: bool

    @property
    def required_supported(self) -> bool:
        return all(
            (
                self.supports_exec,
                self.supports_noninteractive_no_approval,
                self.supports_runtime_config_overrides,
                self.supports_strict_config,
                self.supports_structured_output,
                self.supports_final_output_file,
                self.supports_isolated_configuration,
                self.supports_pinned_cwd,
                self.supports_restricted_filesystem_permissions,
            )
        )


class CodexCliProbe:
    def __init__(self, runner: ProcessRunner, config: CodexProviderConfig) -> None:
        self.runner = runner
        self.config = config
        self._cached: CodexCliCapabilities | None = None

    def inspect(self, cwd: Path) -> CodexCliCapabilities:
        if self._cached is not None:
            return self._cached
        version = self._run((*self._prefix(), "--version"), cwd)
        help_text = self._run((*self._prefix(), "exec", "--help"), cwd)
        sandbox_help = self._run((*self._prefix(), "sandbox", "--help"), cwd)
        supports_permission_profile = "--permission-profile" in sandbox_help
        if supports_permission_profile:
            supports_permission_profile = self._validate_restricted_profile(cwd)
        capabilities = CodexCliCapabilities(
            version=version.strip(),
            supports_exec="Run Codex non-interactively" in help_text,
            supports_noninteractive_no_approval="--config" in help_text,
            supports_runtime_config_overrides="--config" in help_text,
            supports_strict_config="--strict-config" in help_text,
            supports_structured_output="--output-schema" in help_text,
            supports_final_output_file="--output-last-message" in help_text,
            supports_model_override="--model" in help_text,
            supports_isolated_configuration=all(
                flag in help_text
                for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules")
            ),
            supports_pinned_cwd="--cd" in help_text,
            supports_restricted_filesystem_permissions=supports_permission_profile,
        )
        if not capabilities.required_supported:
            raise CodexCliUnsupportedError(
                "local Codex CLI lacks required isolated exec capabilities"
            )
        if self.config.model is not None and not capabilities.supports_model_override:
            raise CodexCliUnsupportedError(
                "local Codex CLI cannot apply the configured model override"
            )
        self._cached = capabilities
        return capabilities

    def _prefix(self) -> tuple[str, ...]:
        return (self.config.executable, *self.config.executable_arguments)

    def _validate_restricted_profile(self, cwd: Path) -> bool:
        command = (
            ("cmd.exe", "/d", "/c", "exit", "0") if os.name == "nt" else ("/bin/sh", "-c", "exit 0")
        )
        values = [
            *self._prefix(),
            "sandbox",
            "--cd",
            str(cwd),
        ]
        for override in restricted_permission_config_overrides():
            values.extend(("--config", override))
        values.extend(("--permission-profile", CODEX_PERMISSION_PROFILE_NAME, *command))
        try:
            result = self.runner.run(
                CommandSpec(
                    argv=tuple(values),
                    cwd=cwd,
                    timeout_seconds=min(max(self.config.timeout_seconds, 5.0), 30.0),
                    max_stdout_bytes=256 * 1024,
                    max_stderr_bytes=256 * 1024,
                    unset_env=sensitive_environment_keys(),
                )
            )
        except ProcessStartError as exc:
            raise CodexCliUnavailableError("configured Codex CLI is unavailable") from exc
        return (
            result.receipt.status is ProcessStatus.SUCCEEDED
            and not result.receipt.stdout_truncated
            and not result.receipt.stderr_truncated
        )

    def _run(self, argv: tuple[str, ...], cwd: Path) -> str:
        try:
            result = self.runner.run(
                CommandSpec(
                    argv=argv,
                    cwd=cwd,
                    timeout_seconds=min(max(self.config.timeout_seconds, 5.0), 30.0),
                    max_stdout_bytes=256 * 1024,
                    max_stderr_bytes=256 * 1024,
                    unset_env=sensitive_environment_keys(),
                )
            )
        except ProcessStartError as exc:
            raise CodexCliUnavailableError("configured Codex CLI is unavailable") from exc
        if (
            result.receipt.status is not ProcessStatus.SUCCEEDED
            or result.receipt.stdout_truncated
            or result.receipt.stderr_truncated
        ):
            raise CodexCliUnavailableError("Codex CLI capability inspection failed")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodexCliUnsupportedError("Codex CLI help/version output is not UTF-8") from exc


def sensitive_environment_keys() -> tuple[str, ...]:
    """Remove ambient credentials while preserving system and local-session paths."""

    cloud_prefixes = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GITHUB_", "GITLAB_", "DB_")
    exact = {"DATABASE_URL", "OPENAI_API_KEY"}
    return tuple(
        sorted(
            key
            for key in os.environ
            if key.upper() in exact
            or key.upper().startswith(cloud_prefixes)
            or key.upper().startswith("GIT_")
            or is_sensitive_environment_key(key)
        )
    )
