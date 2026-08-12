"""Proposal-only Codex CLI adapter."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from agentgraph.infra import (
    CancellationToken,
    CommandSpec,
    GitAdapter,
    ProcessRunner,
    ProcessStatus,
)
from agentgraph.infra.errors import ProcessStartError
from agentgraph.runtime.atomic import atomic_write_bytes
from agentgraph.runtime.codec import canonical_json_bytes
from agentgraph.write import (
    ChangeProviderContext,
    ChangeRequest,
    ChangeSet,
    FileChange,
    normalize_repo_path,
    path_is_allowed,
)
from agentgraph.write.evidence import write_evidence

from .cli import CodexCliCapabilities, CodexCliProbe, sensitive_environment_keys
from .config import CodexProviderConfig
from .errors import (
    CodexCliUnavailableError,
    CodexInvocationError,
    CodexProposalError,
    CodexProviderBlockedError,
    CodexProviderContextError,
    CodexResponseError,
    CodexTimeoutError,
)
from .parser import parse_codex_proposal
from .policy import restricted_permission_config_overrides
from .prompt import build_codex_change_prompt
from .schema import CODEX_PROPOSAL_JSON_SCHEMA, CodexProposal, CodexProposalStatus


class CodexChangeProvider:
    """Invoke exactly one native-profile-isolated Codex proposal turn."""

    def __init__(
        self,
        *,
        process_runner: ProcessRunner | None = None,
        git_adapter: GitAdapter | None = None,
        config: CodexProviderConfig | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.runner = process_runner or ProcessRunner()
        self.config = config or CodexProviderConfig()
        self.git = git_adapter or GitAdapter(self.runner)
        self.cancellation = cancellation
        self.probe = CodexCliProbe(self.runner, self.config)

    def capabilities(self, repository_root: Path) -> CodexCliCapabilities:
        return self.probe.inspect(repository_root)

    def propose(self, request: ChangeRequest, context: ChangeProviderContext) -> ChangeSet:
        repository, provider_dir = self._validate_context(context)
        capabilities = self.probe.inspect(repository.root)
        prompt = build_codex_change_prompt(request)
        prompt_digest = _digest(prompt)
        codex_dir = provider_dir / "codex"
        codex_dir.mkdir()
        schema_path = codex_dir / "schema.json"
        result_path = codex_dir / "final-result.json"
        schema_bytes = canonical_json_bytes(CODEX_PROPOSAL_JSON_SCHEMA)
        atomic_write_bytes(schema_path, schema_bytes)
        argv = self._invocation(repository.root, schema_path, result_path, capabilities)
        try:
            result = self.runner.run(
                CommandSpec(
                    argv=argv,
                    cwd=repository.root,
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
            raw = _read_regular_bounded(result_path, self.config.max_result_bytes)
        except Exception as exc:
            output_error = exc
        evidence_context = _evidence_context(request, context, prompt_digest)
        write_evidence(
            codex_dir / "codex-receipt.json",
            context=evidence_context,
            payload={
                "codex_version": capabilities.version,
                "prompt_digest": prompt_digest,
                "output_digest": None if raw is None else _digest(raw),
                "model": self.config.model,
                "receipt": result.receipt,
            },
        )
        if result.receipt.status is ProcessStatus.TIMED_OUT:
            raise CodexTimeoutError("Codex proposal invocation timed out")
        if result.receipt.status is not ProcessStatus.SUCCEEDED:
            raise CodexInvocationError("Codex proposal invocation failed")
        if result.receipt.stdout_truncated or result.receipt.stderr_truncated:
            raise CodexInvocationError("Codex diagnostic output exceeded its bound")
        if output_error is not None or raw is None:
            raise CodexResponseError(
                "Codex final result is unavailable or unsafe"
            ) from output_error
        proposal = parse_codex_proposal(raw)
        write_evidence(
            codex_dir / "codex-proposal.json",
            context={**evidence_context, "proposal_digest": proposal.digest},
            payload={"proposal": proposal, "proposal_digest": proposal.digest},
        )
        if proposal.status is CodexProposalStatus.BLOCKED:
            assert proposal.reason_code is not None and proposal.message is not None
            raise CodexProviderBlockedError(proposal.reason_code, proposal.message)
        return _materialize(proposal, request, repository.root)

    def _validate_context(self, context: ChangeProviderContext):
        if not isinstance(context, ChangeProviderContext):
            raise CodexProviderContextError("Codex requires ChangeProviderContext")
        root = context.repository_root.resolve(strict=True)
        provider_dir = context.runtime_directory.resolve(strict=True)
        if (
            not root.is_dir()
            or root.is_symlink()
            or not provider_dir.is_dir()
            or provider_dir.is_symlink()
        ):
            raise CodexProviderContextError("Codex provider paths must be real directories")
        try:
            provider_dir.relative_to(root)
        except ValueError:
            pass
        else:
            raise CodexProviderContextError("Codex artifacts must remain outside the repository")
        repository = self.git.discover_repository(root)
        snapshot = self.git.snapshot(repository)
        if repository.root.resolve() != root or snapshot.head_sha != context.baseline_head:
            raise CodexProviderContextError("Codex repository does not match the pinned baseline")
        return repository, provider_dir

    def _invocation(
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


def _materialize(proposal: CodexProposal, request: ChangeRequest, root: Path) -> ChangeSet:
    changes = []
    for item in proposal.changes:
        pure = normalize_repo_path(item.path)
        if not path_is_allowed(item.path, request.allowed_paths):
            error = CodexProposalError("codex_proposal_out_of_scope")
            error.code = "codex_proposal_out_of_scope"
            raise error
        candidate = root.joinpath(*pure.parts)
        _validate_candidate(root, candidate)
        before = hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None
        changes.append(FileChange(item.path, before, item.content))
    return ChangeSet.create(tuple(changes))


def _validate_candidate(root: Path, candidate: Path) -> None:
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise CodexProposalError("Codex proposal targets a symlink")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise CodexProposalError("Codex proposal escapes the repository") from exc
    if candidate.exists() and not candidate.is_file():
        raise CodexProposalError("Codex proposal target is not a regular file")


def _read_regular_bounded(path: Path, limit: int) -> bytes:
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


def _evidence_context(
    request: ChangeRequest, context: ChangeProviderContext, prompt_digest: str
) -> dict[str, object]:
    return {
        "project_id": request.project_id,
        "run_id": context.runtime_directory.parent.name,
        "item_id": request.item_id,
        "scope_id": request.scope_id,
        "pinned_head": context.baseline_head,
        "source_revision": request.source_revision,
        "prompt_digest": prompt_digest,
    }
