"""Canonical operational authority derived from effective configuration."""

from __future__ import annotations

from dataclasses import dataclass

from agentgraph.runtime.codec import sha256_digest

from .models import AgentGraphConfig


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    schema_version: int
    work_source: str
    speckit_workstreams_dir: str
    speckit_active_scope_file: str | None
    agent_provider: str
    codex_model: str | None
    codex_timeout_seconds: int
    codex_max_result_bytes: int
    semantic_review_enabled: bool
    delivery_review_enabled: bool
    max_repair_cycles: int
    max_work_items_per_run: int
    checkpoint_ttl_seconds: int
    validation_timeout_seconds: int
    max_steps: int
    commit_mode: str
    publish_enabled: bool
    publish_provider: str
    publish_remote: str
    publish_draft: bool
    codex_executable_selector: str
    digest: str

    @classmethod
    def create(cls, config: AgentGraphConfig, codex_executable: str) -> ExecutionProfile:
        values = {
            "schema_version": 1,
            "work_source": config.work.source,
            "speckit_workstreams_dir": config.work.speckit.workstreams_dir,
            "speckit_active_scope_file": config.work.speckit.active_scope_file,
            "agent_provider": config.agents.provider,
            "codex_model": config.agents.codex.model,
            "codex_timeout_seconds": config.agents.codex.timeout_seconds,
            "codex_max_result_bytes": config.agents.codex.max_result_bytes,
            "semantic_review_enabled": config.review.semantic,
            "delivery_review_enabled": config.review.delivery,
            "max_repair_cycles": config.policy.max_repair_cycles,
            "max_work_items_per_run": config.policy.max_work_items_per_run,
            "checkpoint_ttl_seconds": config.policy.checkpoint_ttl_seconds,
            "validation_timeout_seconds": config.policy.validation_timeout_seconds,
            "max_steps": config.policy.max_steps,
            "commit_mode": config.policy.commit_mode,
            "publish_enabled": config.publish.enabled,
            "publish_provider": config.publish.provider,
            "publish_remote": config.publish.remote,
            "publish_draft": config.publish.draft,
            "codex_executable_selector": codex_executable,
        }
        return cls(**values, digest=sha256_digest(values))

    def __post_init__(self) -> None:
        values = {
            name: getattr(self, name) for name in self.__dataclass_fields__ if name != "digest"
        }
        if self.schema_version != 1 or self.digest != sha256_digest(values):
            raise ValueError("execution profile digest is invalid")
