from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from agentgraph.config import ConfigError, load_project_config, load_project_config_snapshot


def test_loads_typed_canonical_config(config_root: Path) -> None:
    config = load_project_config(config_root)

    assert config.version == 1
    assert config.work.source == "speckit"
    assert config.agents.codex.timeout_seconds == 900
    assert config.policy.max_repair_cycles == 2
    assert config.publish.remote == "origin"


def test_config_snapshot_digest_is_from_the_exact_bytes_parsed(
    config_root: Path, monkeypatch
) -> None:
    path = config_root / ".agentgraph.yml"
    raw = path.read_bytes()
    read_bytes = Path.read_bytes

    def read_then_change_config(candidate: Path) -> bytes:
        content = read_bytes(candidate)
        if candidate == path:
            path.write_bytes(raw.replace(b"max_repair_cycles: 2", b"max_repair_cycles: 1"))
        return content

    monkeypatch.setattr(Path, "read_bytes", read_then_change_config)

    loaded = load_project_config_snapshot(config_root)

    assert loaded.config.policy.max_repair_cycles == 2
    assert loaded.raw_content_digest == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert b"max_repair_cycles: 1" in read_bytes(path)


def _replace_section(config_text: str, start: str, end: str, replacement: str) -> str:
    before, remainder = config_text.split(start, 1)
    _old, after = remainder.split(end, 1)
    return before + replacement + end + after


def test_minimal_speckit_config_uses_defaults(config_root: Path, config_text: str) -> None:
    path = config_root / ".agentgraph.yml"
    path.write_text(
        _replace_section(config_text, "work:\n", "agents:\n", "work:\n  source: speckit\n"),
        encoding="utf-8",
    )

    config = load_project_config(config_root)

    assert config.work.speckit.workstreams_dir == ".specify/workstreams"
    assert config.work.speckit.active_scope_file == ".specify/runtime/active-epic"


def test_partial_speckit_config_preserves_remaining_default(
    config_root: Path, config_text: str
) -> None:
    path = config_root / ".agentgraph.yml"
    replacement = "work:\n  source: speckit\n  speckit:\n    active_scope_file: null\n"
    path.write_text(
        _replace_section(config_text, "work:\n", "agents:\n", replacement),
        encoding="utf-8",
    )

    config = load_project_config(config_root)

    assert config.work.speckit.workstreams_dir == ".specify/workstreams"
    assert config.work.speckit.active_scope_file is None


def test_minimal_codex_config_uses_defaults(config_root: Path, config_text: str) -> None:
    path = config_root / ".agentgraph.yml"
    path.write_text(
        _replace_section(config_text, "agents:\n", "review:\n", "agents:\n  provider: codex\n"),
        encoding="utf-8",
    )

    config = load_project_config(config_root)

    assert config.agents.codex.model is None
    assert config.agents.codex.timeout_seconds == 900
    assert config.agents.codex.max_result_bytes == 4 * 1024 * 1024


def test_partial_codex_config_preserves_remaining_defaults(
    config_root: Path, config_text: str
) -> None:
    path = config_root / ".agentgraph.yml"
    replacement = "agents:\n  provider: codex\n  codex:\n    model: gpt-example\n"
    path.write_text(
        _replace_section(config_text, "agents:\n", "review:\n", replacement),
        encoding="utf-8",
    )

    config = load_project_config(config_root)

    assert config.agents.codex.model == "gpt-example"
    assert config.agents.codex.timeout_seconds == 900
    assert config.agents.codex.max_result_bytes == 4 * 1024 * 1024


@pytest.mark.parametrize(
    ("fragment", "code"),
    [
        (
            "policy:\n  max_repair_cycles: 1\n  max_repair_cycles: 2\n",
            "yaml_duplicate_key",
        ),
        ("agents:\n  provider: codex\n  executable: powershell.exe\n", "config_unknown_field"),
        ("work:\n  source: other\n", "unsupported_work_source"),
        (
            "agents:\n  provider: codex\n  codex:\n    executable: evil\n",
            "config_unknown_field",
        ),
    ],
)
def test_rejects_unsafe_or_ambiguous_fields(
    tmp_path: Path, config_text: str, fragment: str, code: str
) -> None:
    sections = {
        "policy:": "publish:",
        "agents:": "review:",
        "work:": "agents:",
    }
    start = next(key for key in sections if fragment.startswith(key))
    before, remainder = config_text.split(start, 1)
    _old, after = remainder.split(sections[start], 1)
    (tmp_path / ".agentgraph.yml").write_text(
        before + fragment + sections[start] + after, encoding="utf-8"
    )

    with pytest.raises(ConfigError) as error:
        load_project_config(tmp_path)

    assert error.value.code == code


@pytest.mark.parametrize(
    "body",
    [
        "version: 1\na: &authority {x: 1}\nb: *authority\n",
        "version: 1\na: !!python/object/apply:os.system ['echo unsafe']\n",
        "---\nversion: 1\n---\nversion: 1\n",
    ],
)
def test_rejects_aliases_unsafe_tags_and_multiple_documents(tmp_path: Path, body: str) -> None:
    (tmp_path / ".agentgraph.yml").write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_project_config(tmp_path)


def test_rejects_merge_key_without_alias(tmp_path: Path) -> None:
    (tmp_path / ".agentgraph.yml").write_text(
        "version: 1\nwork:\n  <<: {source: speckit}\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError) as error:
        load_project_config(tmp_path)

    assert error.value.code == "yaml_merge_key_forbidden"


def test_rejects_path_escape(tmp_path: Path, config_text: str) -> None:
    config_text = config_text.replace(".specify/workstreams", "../outside")
    (tmp_path / ".agentgraph.yml").write_text(config_text, encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_project_config(tmp_path)

    assert error.value.code == "speckit_path_invalid"


def test_rejects_symlink(config_root: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "config.yml"
    outside.write_text("sentinel", encoding="utf-8")
    config = config_root / ".agentgraph.yml"
    config.unlink()
    try:
        config.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ConfigError) as error:
        load_project_config(config_root)

    assert error.value.code == "config_not_regular"
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_rejects_oversized_and_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / ".agentgraph.yml"
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ConfigError) as oversized:
        load_project_config(tmp_path)
    assert oversized.value.code == "config_too_large"

    path.write_bytes(b"version: \xff")
    with pytest.raises(ConfigError) as encoding:
        load_project_config(tmp_path)
    assert encoding.value.code == "config_invalid_utf8"


def test_strict_boolean_and_integer_types(config_root: Path, config_text: str) -> None:
    path = config_root / ".agentgraph.yml"
    path.write_text(config_text.replace("semantic: true", 'semantic: "true"'), encoding="utf-8")
    with pytest.raises(ConfigError) as boolean:
        load_project_config(config_root)
    assert boolean.value.code == "config_type_invalid"

    path.write_text(config_text.replace("max_steps: 30", "max_steps: true"), encoding="utf-8")
    with pytest.raises(ConfigError) as integer:
        load_project_config(config_root)
    assert integer.value.code == "config_type_invalid"


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("delivery: true", "delivery: false", "delivery_review_required_in_v1"),
        ("enabled: true", "enabled: false", "publish_required_in_v1"),
        ("draft: true", "draft: false", "unsupported_publish_mode"),
        ("provider: codex", "provider: other", "unsupported_agent_provider"),
        ("provider: github", "provider: other", "unsupported_publish_provider"),
    ],
)
def test_rejects_unsupported_modes_and_combinations(
    config_root: Path, config_text: str, old: str, new: str, code: str
) -> None:
    (config_root / ".agentgraph.yml").write_text(config_text.replace(old, new), encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_project_config(config_root)

    assert error.value.code == code


def test_delivery_review_false_wins_when_publish_is_also_disabled(
    config_root: Path, config_text: str
) -> None:
    path = config_root / ".agentgraph.yml"
    path.write_text(
        config_text.replace("delivery: true", "delivery: false").replace(
            "enabled: true", "enabled: false"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_project_config(config_root)

    assert error.value.code == "delivery_review_required_in_v1"


def test_config_is_not_modified(config_root: Path) -> None:
    path = config_root / ".agentgraph.yml"
    before = path.read_bytes()
    before_stat = os.stat(path)

    load_project_config(config_root)

    assert path.read_bytes() == before
    assert os.stat(path).st_mtime_ns == before_stat.st_mtime_ns
