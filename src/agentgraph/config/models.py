"""Immutable typed v1 project configuration contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpecKitConfig:
    workstreams_dir: str = ".specify/workstreams"
    active_scope_file: str | None = ".specify/runtime/active-epic"


@dataclass(frozen=True, slots=True)
class WorkConfig:
    source: str
    speckit: SpecKitConfig


@dataclass(frozen=True, slots=True)
class CodexConfig:
    model: str | None = None
    timeout_seconds: int = 900
    max_result_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AgentsConfig:
    provider: str
    codex: CodexConfig


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    semantic: bool
    delivery: bool


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    max_repair_cycles: int
    max_work_items_per_run: int
    checkpoint_ttl_seconds: int
    validation_timeout_seconds: int
    max_steps: int
    commit_mode: str


@dataclass(frozen=True, slots=True)
class PublishConfig:
    enabled: bool
    provider: str
    remote: str
    draft: bool


@dataclass(frozen=True, slots=True)
class AgentGraphConfig:
    version: int
    work: WorkConfig
    agents: AgentsConfig
    review: ReviewConfig
    policy: PolicyConfig
    publish: PublishConfig
