"""Production v1 composition root and thin application service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.config import AgentGraphConfig, ExecutionProfile, load_project_config
from agentgraph.core import CheckpointOutcome
from agentgraph.infra import GitAdapter, ProcessRunner
from agentgraph.providers.codex import CodexAgentProvider, CodexChangeProvider, CodexProviderConfig
from agentgraph.runtime import ProjectRegistry, RuntimePaths
from agentgraph.write import (
    ChangeProvider,
    CheckpointError,
    GitHubRemoteProvider,
    RemoteProvider,
    WriteSliceReport,
    WriteSliceRequest,
    WriteSliceRunner,
)

from .errors import CliError
from .models import SafeCheckpoint
from .status import StatusReport, StatusService


@dataclass(frozen=True, slots=True)
class ProviderOverrides:
    process_runner: ProcessRunner | None = None
    git_adapter: GitAdapter | None = None
    change_provider: ChangeProvider | None = None
    general_agent_provider: object | None = None
    semantic_review_provider: object | None = None
    delivery_review_provider: object | None = None
    remote_provider: RemoteProvider | None = None


@dataclass(frozen=True, slots=True)
class AgentGraphApplication:
    repository_root: Path
    config: AgentGraphConfig
    profile: ExecutionProfile
    runtime_paths: RuntimePaths
    registry: ProjectRegistry
    process_runner: ProcessRunner
    git_adapter: GitAdapter
    work_source: SpecKitAdapter
    runner: WriteSliceRunner
    status_service: StatusService
    general_agent_provider: object
    semantic_review_provider: object | None
    delivery_review_provider: object | None

    def run(self, scope_id: str | None, parent_scope_id: str | None) -> WriteSliceReport:
        return self.runner.run(WriteSliceRequest(scope_id, parent_scope_id))

    def resume(self, run_id: str | None) -> WriteSliceReport:
        _project_id, selected = self.status_service.resolve_run_id(run_id)
        return self.runner.resume(selected)

    def status(self, run_id: str | None) -> StatusReport:
        return self.status_service.inspect(run_id)

    def show_checkpoint(self, run_id: str | None) -> tuple[str, SafeCheckpoint]:
        _project_id, selected = self.status_service.resolve_run_id(run_id)
        materialized = self.status_service.materialized_checkpoint(selected)
        return selected, materialized.view

    def submit_checkpoint(
        self,
        run_id: str | None,
        *,
        outcome: CheckpointOutcome,
        actor: str,
    ) -> tuple[str, SafeCheckpoint]:
        if not isinstance(actor, str) or not actor.strip() or "\x00" in actor or len(actor) > 256:
            raise CliError("checkpoint_actor_invalid", "checkpoint actor is invalid")
        _project_id, selected = self.status_service.resolve_run_id(run_id)
        materialized = self.status_service.materialized_checkpoint(selected)
        try:
            self.runner.submit_checkpoint(
                selected,
                checkpoint_id=materialized.view.checkpoint_id,
                nonce=materialized.nonce,
                outcome=outcome,
                actor=actor,
            )
        except CheckpointError as exc:
            raise CliError(exc.code, str(exc)) from exc
        return selected, materialized.view


def build_application(
    repository_path: Path | str,
    *,
    runtime_home: Path | str | None = None,
    codex_executable: str | None = None,
    provider_overrides: ProviderOverrides | None = None,
) -> AgentGraphApplication:
    overrides = provider_overrides or ProviderOverrides()
    if (
        overrides.git_adapter is not None
        and overrides.process_runner is not None
        and overrides.git_adapter.runner is not overrides.process_runner
    ):
        raise ValueError("injected GitAdapter and ProcessRunner must share one runner")
    processes = (
        overrides.process_runner
        or (overrides.git_adapter.runner if overrides.git_adapter is not None else None)
        or ProcessRunner()
    )
    git = overrides.git_adapter or GitAdapter(processes)
    repository = git.discover_repository(repository_path)
    config = load_project_config(repository.root)
    executable = _codex_executable(codex_executable)
    profile = ExecutionProfile.create(config, executable)
    paths = RuntimePaths.resolve(runtime_home)
    paths.require_external_to(repository.root)
    registry = ProjectRegistry(paths)
    layout = SpecKitLayout(
        repository.root,
        config.work.speckit.workstreams_dir,
        config.work.speckit.active_scope_file,
    )
    work_source = SpecKitAdapter(layout)
    codex_config = CodexProviderConfig(
        executable=executable,
        executable_arguments=(),
        timeout_seconds=config.agents.codex.timeout_seconds,
        model=config.agents.codex.model,
        max_result_bytes=config.agents.codex.max_result_bytes,
    )
    change_provider = overrides.change_provider or CodexChangeProvider(
        process_runner=processes, git_adapter=git, config=codex_config
    )
    general = overrides.general_agent_provider or CodexAgentProvider(
        process_runner=processes, config=codex_config
    )
    semantic = (
        None
        if not config.review.semantic
        else overrides.semantic_review_provider
        or CodexAgentProvider(process_runner=processes, config=codex_config)
    )
    delivery = (
        None
        if not config.review.delivery
        else overrides.delivery_review_provider
        or CodexAgentProvider(process_runner=processes, config=codex_config)
    )
    remote = (
        None if not config.publish.enabled else overrides.remote_provider or GitHubRemoteProvider()
    )
    runner = WriteSliceRunner(
        repository.root,
        work_source,
        change_provider,
        agent_provider=general,
        review_agent_provider=semantic,
        delivery_review_agent_provider=delivery,
        remote_provider=remote,
        publish_remote_name=config.publish.remote,
        git_adapter=git,
        project_registry=registry,
        process_runner=processes,
        runtime_paths=paths,
        validation_timeout_seconds=config.policy.validation_timeout_seconds,
        max_steps=config.policy.max_steps,
        max_repair_cycles=config.policy.max_repair_cycles,
        max_work_items_per_run=config.policy.max_work_items_per_run,
        checkpoint_ttl_seconds=config.policy.checkpoint_ttl_seconds,
        execution_profile_digest=profile.digest,
        execution_profile_payload=profile,
    )
    status = StatusService(repository.root, paths, registry, profile)
    return AgentGraphApplication(
        repository.root,
        config,
        profile,
        paths,
        registry,
        processes,
        git,
        work_source,
        runner,
        status,
        general,
        semantic,
        delivery,
    )


def _codex_executable(explicit: str | None) -> str:
    value = (
        explicit if explicit is not None else os.environ.get("AGENTGRAPH_CODEX_EXECUTABLE", "codex")
    )
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 4096:
        raise CliError("codex_executable_invalid", "Codex executable selector is invalid")
    return value
