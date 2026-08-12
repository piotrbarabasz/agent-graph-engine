"""Shared secure structured-output runtime for Codex providers."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentgraph.infra import (
    CancellationToken,
    CommandReceipt,
    CommandSpec,
    ProcessRunner,
    ProcessStatus,
)
from agentgraph.infra.errors import ProcessStartError
from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.runtime.codec import canonical_json_bytes
from agentgraph.write.evidence import write_evidence

from .cli import CodexCliCapabilities, CodexCliProbe, sensitive_environment_keys
from .config import CodexProviderConfig
from .errors import (
    CodexCliUnavailableError,
    CodexInvocationError,
    CodexResponseError,
    CodexTimeoutError,
)
from .policy import restricted_permission_config_overrides


@dataclass(frozen=True, slots=True)
class CodexStructuredResult:
    raw: bytes
    capabilities: CodexCliCapabilities
    prompt_digest: str
    output_digest: str
    receipt: CommandReceipt


class CodexInvocationRuntime:
    """Execute one strict Codex turn under the accepted M007 security boundary."""

    def __init__(
        self,
        runner: ProcessRunner,
        config: CodexProviderConfig,
        *,
        cancellation: CancellationToken | None = None,
        probe: CodexCliProbe | None = None,
    ) -> None:
        self.runner = runner
        self.config = config
        self.cancellation = cancellation
        self.probe = probe or CodexCliProbe(runner, config)

    def invoke_structured(
        self,
        *,
        prompt: bytes,
        schema: Any,
        repository_root: Path,
        artifact_directory: Path,
        evidence_context: dict[str, Any],
        receipt_name: str = "codex-receipt.json",
    ) -> CodexStructuredResult:
        root = repository_root.resolve(strict=True)
        artifact = artifact_directory.resolve(strict=False)
        if not root.is_dir() or root.is_symlink():
            raise CodexResponseError("Codex repository root is unsafe")
        if artifact.is_symlink() or (artifact.exists() and not artifact.is_dir()):
            raise CodexResponseError("Codex artifact directory is unsafe")
        artifact.mkdir(parents=True, exist_ok=True)
        schema_path = artifact / "schema.json"
        result_path = artifact / "final-result.json"
        receipt_path = artifact / receipt_name
        if any(
            path.exists() or path.is_symlink() for path in (schema_path, result_path, receipt_path)
        ):
            raise CodexResponseError("Codex structured invocation artifacts already exist")
        atomic_write_bytes(schema_path, canonical_json_bytes(schema))
        capabilities = self.probe.inspect(root)
        prompt_digest = _digest(prompt)
        argv = self.invocation(root, schema_path, result_path, capabilities)
        try:
            result = self.runner.run(
                CommandSpec(
                    argv=argv,
                    cwd=root,
                    timeout_seconds=self.config.timeout_seconds,
                    stdin=prompt,
                    max_stdout_bytes=256 * 1024,
                    max_stderr_bytes=256 * 1024,
                    unset_env=sensitive_environment_keys(),
                ),
                cancellation=self.cancellation,
            )
        except ProcessStartError as exc:
            raise CodexCliUnavailableError("configured Codex CLI is unavailable") from exc
        raw: bytes | None = None
        output_error: Exception | None = None
        try:
            raw = read_regular_bounded(result_path, self.config.max_result_bytes)
        except Exception as exc:
            output_error = exc
        output_digest = None if raw is None else _digest(raw)
        write_evidence(
            receipt_path,
            context=evidence_context,
            payload={
                "codex_version": capabilities.version,
                "prompt_digest": prompt_digest,
                "output_digest": output_digest,
                "model": self.config.model,
                "receipt": result.receipt,
            },
        )
        if result.receipt.status is ProcessStatus.TIMED_OUT:
            raise CodexTimeoutError("Codex structured invocation timed out")
        if result.receipt.status is not ProcessStatus.SUCCEEDED:
            raise CodexInvocationError("Codex structured invocation failed")
        if result.receipt.stdout_truncated or result.receipt.stderr_truncated:
            raise CodexInvocationError("Codex diagnostic output exceeded its bound")
        if output_error is not None or raw is None or output_digest is None:
            raise CodexResponseError(
                "Codex final result is unavailable or unsafe"
            ) from output_error
        return CodexStructuredResult(
            raw, capabilities, prompt_digest, output_digest, result.receipt
        )

    def invocation(
        self,
        repository_root: Path,
        schema_path: Path,
        result_path: Path,
        capabilities: CodexCliCapabilities,
    ) -> tuple[str, ...]:
        if not capabilities.required_supported:
            raise AssertionError("unsupported Codex capabilities escaped probe")
        values = [
            self.config.executable,
            *self.config.executable_arguments,
            "exec",
            "--cd",
            str(repository_root),
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
        for override in (
            *restricted_permission_config_overrides(),
            'approval_policy="never"',
            "mcp_servers={}",
            'web_search="disabled"',
        ):
            values.extend(("--config", override))
        values.extend(
            (
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "--color",
                "never",
            )
        )
        if self.config.model is not None:
            values.extend(("--model", self.config.model))
        values.append("-")
        return tuple(values)


def read_regular_bounded(path: Path, limit: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CodexResponseError("Codex final result file is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise CodexResponseError("Codex final result file is unsafe or oversized")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise CodexResponseError("Codex final result could not be read safely") from exc
    if len(raw) > limit:
        raise CodexResponseError("Codex final result exceeds its bound")
    return raw


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
