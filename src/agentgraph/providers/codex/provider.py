"""Proposal-only Codex CLI adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path

from agentgraph.infra import (
    CancellationToken,
    GitAdapter,
    ProcessRunner,
)
from agentgraph.write import (
    ChangeProviderContext,
    ChangeRequest,
    ChangeSet,
    FileChange,
    normalize_repo_path,
    path_is_allowed,
)
from agentgraph.write.evidence import write_evidence

from .cli import CodexCliCapabilities, CodexCliProbe
from .config import CodexProviderConfig
from .errors import (
    CodexProposalError,
    CodexProviderBlockedError,
    CodexProviderContextError,
)
from .parser import parse_codex_proposal
from .prompt import build_codex_change_prompt
from .runtime import CodexInvocationRuntime
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
        self.runtime = CodexInvocationRuntime(
            self.runner, self.config, cancellation=self.cancellation, probe=self.probe
        )

    def capabilities(self, repository_root: Path) -> CodexCliCapabilities:
        return self.probe.inspect(repository_root)

    def propose(self, request: ChangeRequest, context: ChangeProviderContext) -> ChangeSet:
        repository, provider_dir = self._validate_context(context)
        prompt = build_codex_change_prompt(request)
        codex_dir = provider_dir / "codex"
        evidence_context = _evidence_context(request, context)
        result = self.runtime.invoke_structured(
            prompt=prompt,
            schema=CODEX_PROPOSAL_JSON_SCHEMA,
            repository_root=repository.root,
            artifact_directory=codex_dir,
            evidence_context=evidence_context,
        )
        proposal = parse_codex_proposal(result.raw)
        write_evidence(
            codex_dir / "codex-proposal.json",
            context={
                **evidence_context,
                "prompt_digest": result.prompt_digest,
                "proposal_digest": proposal.digest,
            },
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
        return self.runtime.invocation(repository_root, schema_path, result_path, capabilities)


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


def _evidence_context(
    request: ChangeRequest, context: ChangeProviderContext, prompt_digest: str | None = None
) -> dict[str, object]:
    values = {
        "project_id": request.project_id,
        "run_id": context.run_id or context.runtime_directory.parent.name,
        "node_id": context.node_id,
        "node_attempt_id": context.node_attempt_id,
        "provider_invocation_id": context.provider_invocation_id,
        "repair_cycle": context.repair_cycle,
        "item_id": request.item_id,
        "scope_id": request.scope_id,
        "pinned_head": context.baseline_head,
        "source_revision": request.source_revision,
    }
    if prompt_digest is not None:
        values["prompt_digest"] = prompt_digest
    return values
