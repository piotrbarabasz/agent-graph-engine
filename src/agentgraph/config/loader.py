"""Strict loader for the one configuration file owned by a canonical Git root."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent, DocumentStartEvent, ScalarEvent
from yaml.loader import SafeLoader

from agentgraph.adapters.speckit import SpecKitLayout
from agentgraph.work import WorkSourceConfigurationError, WorkSourcePathError

from .errors import ConfigError
from .models import (
    AgentGraphConfig,
    AgentsConfig,
    CodexConfig,
    PolicyConfig,
    PublishConfig,
    ReviewConfig,
    SpecKitConfig,
    WorkConfig,
)

CONFIG_NAME = ".agentgraph.yml"
MAX_CONFIG_BYTES = 64 * 1024
MAX_MODEL_LENGTH = 256


class _UniqueSafeLoader(SafeLoader):
    pass


def _construct_unique_mapping(loader: SafeLoader, node, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "mapping", node.start_mark, "unhashable key", key_node.start_mark
            ) from exc
        if duplicate:
            raise ConstructorError(
                "mapping", node.start_mark, f"duplicate key: {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_project_config(repository_root: Path | str) -> AgentGraphConfig:
    root = Path(repository_root).expanduser().resolve()
    path = root / CONFIG_NAME
    _verify_config_file(root, path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(
            "config_unreadable", "configuration file cannot be read", path=path
        ) from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("config_too_large", "configuration exceeds 64 KiB", path=path)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            "config_invalid_utf8", "configuration must be strict UTF-8", path=path
        ) from exc
    if "\x00" in text:
        raise ConfigError("config_nul_forbidden", "configuration contains NUL", path=path)
    document = _parse_yaml(text, path)
    return _parse_config(document, root, path)


def _verify_config_file(root: Path, path: Path) -> None:
    if not root.is_dir():
        raise ConfigError(
            "repository_root_invalid", "canonical repository root is invalid", path=path
        )
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ConfigError(
            "config_not_found", "configuration file does not exist", path=path
        ) from exc
    except OSError as exc:
        raise ConfigError(
            "config_unreadable", "configuration metadata is unreadable", path=path
        ) from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    if stat.S_ISLNK(metadata.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ConfigError("config_not_regular", "configuration must not be a link", path=path)
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigError("config_not_regular", "configuration must be a regular file", path=path)
    if metadata.st_size > MAX_CONFIG_BYTES:
        raise ConfigError("config_too_large", "configuration exceeds 64 KiB", path=path)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ConfigError(
            "config_path_invalid", "configuration is outside the repository root", path=path
        ) from exc
    if resolved.parent != root:
        raise ConfigError(
            "config_path_invalid", "configuration is not at the exact repository root", path=path
        )


def _parse_yaml(text: str, path: Path) -> object:
    try:
        events = tuple(yaml.parse(text, Loader=SafeLoader))
        if any(isinstance(event, AliasEvent) or getattr(event, "anchor", None) for event in events):
            raise ConfigError(
                "yaml_alias_forbidden", "YAML anchors and aliases are forbidden", path=path
            )
        if any(isinstance(event, ScalarEvent) and event.value == "<<" for event in events):
            raise ConfigError(
                "yaml_merge_key_forbidden", "YAML merge keys are forbidden", path=path
            )
        if sum(isinstance(event, DocumentStartEvent) for event in events) > 1:
            raise ConfigError(
                "yaml_multiple_documents", "exactly one YAML document is required", path=path
            )
        document = yaml.load(text, Loader=_UniqueSafeLoader)
    except ConfigError:
        raise
    except ConstructorError as exc:
        message = "duplicate YAML key" if "duplicate key" in str(exc) else "unsafe or invalid YAML"
        code = "yaml_duplicate_key" if "duplicate key" in str(exc) else "yaml_invalid"
        raise ConfigError(code, message, path=path) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(
            "yaml_invalid", "configuration is not valid safe YAML", path=path
        ) from exc
    return document


def _parse_config(value: object, root: Path, path: Path) -> AgentGraphConfig:
    data = _mapping(value, "configuration", path)
    _fields(data, {"version", "work", "agents", "review", "policy", "publish"}, path)
    version = _integer(_required(data, "version", path), "version", path)
    if version != 1:
        raise ConfigError(
            "unsupported_config_version", "only configuration version 1 is supported", path=path
        )
    work = _parse_work(_required(data, "work", path), root, path)
    agents = _parse_agents(_required(data, "agents", path), path)
    review = _parse_review(_required(data, "review", path), path)
    policy = _parse_policy(_required(data, "policy", path), path)
    publish = _parse_publish(_required(data, "publish", path), path)
    if not review.delivery:
        raise ConfigError(
            "delivery_review_required_in_v1",
            "configuration version 1 requires delivery review",
            path=path,
        )
    if not publish.enabled:
        raise ConfigError(
            "publish_required_in_v1",
            "configuration version 1 requires draft pull-request publication",
            path=path,
        )
    return AgentGraphConfig(version, work, agents, review, policy, publish)


def _parse_work(value: object, root: Path, path: Path) -> WorkConfig:
    data = _mapping(value, "work", path)
    _fields(data, {"source", "speckit"}, path, required={"source"})
    source = _string(data["source"], "work.source", path)
    if source != "speckit":
        raise ConfigError(
            "unsupported_work_source", "only the speckit work source is supported", path=path
        )
    raw = data.get("speckit", {})
    speckit = _mapping(raw, "work.speckit", path)
    _fields(speckit, {"workstreams_dir", "active_scope_file"}, path, required=set())
    workstreams = _string(
        speckit.get("workstreams_dir", ".specify/workstreams"),
        "work.speckit.workstreams_dir",
        path,
    )
    active_raw = speckit.get("active_scope_file", ".specify/runtime/active-epic")
    active = (
        None if active_raw is None else _string(active_raw, "work.speckit.active_scope_file", path)
    )
    try:
        layout = SpecKitLayout(root, workstreams, active)
    except (WorkSourceConfigurationError, WorkSourcePathError) as exc:
        raise ConfigError("speckit_path_invalid", str(exc), path=path) from exc
    return WorkConfig(source, SpecKitConfig(layout.workstreams_dir, layout.active_scope_file))


def _parse_agents(value: object, path: Path) -> AgentsConfig:
    data = _mapping(value, "agents", path)
    _fields(data, {"provider", "codex"}, path, required={"provider"})
    provider = _string(data["provider"], "agents.provider", path)
    if provider != "codex":
        raise ConfigError(
            "unsupported_agent_provider", "only the codex agent provider is supported", path=path
        )
    raw = _mapping(data.get("codex", {}), "agents.codex", path)
    _fields(raw, {"model", "timeout_seconds", "max_result_bytes"}, path, required=set())
    model_raw = raw.get("model")
    model = None if model_raw is None else _string(model_raw, "agents.codex.model", path)
    if model is not None and len(model) > MAX_MODEL_LENGTH:
        raise ConfigError("config_value_out_of_range", "agents.codex.model is too long", path=path)
    timeout = _bounded_int(
        raw.get("timeout_seconds", 900), "agents.codex.timeout_seconds", 30, 3600, path
    )
    result_bytes = _bounded_int(
        raw.get("max_result_bytes", 4 * 1024 * 1024),
        "agents.codex.max_result_bytes",
        64 * 1024,
        16 * 1024 * 1024,
        path,
    )
    return AgentsConfig(provider, CodexConfig(model, timeout, result_bytes))


def _parse_review(value: object, path: Path) -> ReviewConfig:
    data = _mapping(value, "review", path)
    _fields(data, {"semantic", "delivery"}, path, required={"semantic", "delivery"})
    return ReviewConfig(
        _boolean(data["semantic"], "review.semantic", path),
        _boolean(data["delivery"], "review.delivery", path),
    )


def _parse_policy(value: object, path: Path) -> PolicyConfig:
    names = {
        "max_repair_cycles",
        "max_work_items_per_run",
        "checkpoint_ttl_seconds",
        "validation_timeout_seconds",
        "max_steps",
        "commit_mode",
    }
    data = _mapping(value, "policy", path)
    _fields(data, names, path, required=names)
    repairs = _integer(data["max_repair_cycles"], "policy.max_repair_cycles", path)
    if repairs not in {0, 1, 2}:
        raise ConfigError(
            "config_value_out_of_range", "policy.max_repair_cycles must be 0, 1, or 2", path=path
        )
    commit_mode = _string(data["commit_mode"], "policy.commit_mode", path)
    if commit_mode != "per_work_item":
        raise ConfigError(
            "unsupported_commit_mode", "only per_work_item commit mode is supported", path=path
        )
    return PolicyConfig(
        repairs,
        _bounded_int(data["max_work_items_per_run"], "policy.max_work_items_per_run", 1, 20, path),
        _bounded_int(
            data["checkpoint_ttl_seconds"], "policy.checkpoint_ttl_seconds", 60, 86400, path
        ),
        _bounded_int(
            data["validation_timeout_seconds"], "policy.validation_timeout_seconds", 1, 3600, path
        ),
        _bounded_int(data["max_steps"], "policy.max_steps", 1, 1000, path),
        commit_mode,
    )


def _parse_publish(value: object, path: Path) -> PublishConfig:
    names = {"enabled", "provider", "remote", "draft"}
    data = _mapping(value, "publish", path)
    _fields(data, names, path, required=names)
    enabled = _boolean(data["enabled"], "publish.enabled", path)
    provider = _string(data["provider"], "publish.provider", path)
    if provider != "github":
        raise ConfigError(
            "unsupported_publish_provider",
            "only the github publish provider is supported",
            path=path,
        )
    remote = _string(data["remote"], "publish.remote", path)
    if remote.startswith("-") or len(remote) > 256:
        raise ConfigError("publish_remote_invalid", "publish.remote is invalid", path=path)
    draft = _boolean(data["draft"], "publish.draft", path)
    if not draft:
        raise ConfigError(
            "unsupported_publish_mode", "only draft pull requests are supported", path=path
        )
    return PublishConfig(enabled, provider, remote, draft)


def _mapping(value: object, name: str, path: Path) -> dict[str, object]:
    if type(value) is not dict or not all(type(key) is str for key in value):
        raise ConfigError("config_type_invalid", f"{name} must be a mapping", path=path)
    if "<<" in value:
        raise ConfigError("yaml_merge_key_forbidden", "YAML merge keys are forbidden", path=path)
    return value


def _fields(
    data: dict[str, object],
    allowed: set[str],
    path: Path,
    *,
    required: set[str] | None = None,
) -> None:
    unknown = set(data) - allowed
    if unknown:
        name = sorted(unknown)[0]
        raise ConfigError("config_unknown_field", f"unknown configuration field: {name}", path=path)
    required_fields = allowed if required is None else required
    missing = required_fields - set(data)
    if missing:
        name = sorted(missing)[0]
        raise ConfigError(
            "config_missing_field", f"required configuration field is missing: {name}", path=path
        )


def _required(data: dict[str, object], name: str, path: Path) -> object:
    if name not in data:
        raise ConfigError(
            "config_missing_field", f"required configuration field is missing: {name}", path=path
        )
    return data[name]


def _string(value: object, name: str, path: Path) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ConfigError(
            "config_type_invalid", f"{name} must be a non-empty NUL-free string", path=path
        )
    return value


def _integer(value: object, name: str, path: Path) -> int:
    if type(value) is not int:
        raise ConfigError("config_type_invalid", f"{name} must be an integer", path=path)
    return value


def _bounded_int(value: object, name: str, minimum: int, maximum: int, path: Path) -> int:
    parsed = _integer(value, name, path)
    if not minimum <= parsed <= maximum:
        raise ConfigError(
            "config_value_out_of_range",
            f"{name} must be between {minimum} and {maximum}",
            path=path,
        )
    return parsed


def _boolean(value: object, name: str, path: Path) -> bool:
    if type(value) is not bool:
        raise ConfigError("config_type_invalid", f"{name} must be a boolean", path=path)
    return value
