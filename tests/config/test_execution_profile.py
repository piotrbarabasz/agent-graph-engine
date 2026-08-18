from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agentgraph.config import ExecutionProfile, load_project_config
from agentgraph.write import WriteRunInputs, write_run_inputs_digest


def test_profile_digest_ignores_comments_and_key_order(config_root: Path) -> None:
    first = ExecutionProfile.create(load_project_config(config_root), "codex-host")
    path = config_root / ".agentgraph.yml"
    text = path.read_text(encoding="utf-8")
    text = "# comment-only authority change\n" + text.replace(
        "  semantic: true\n  delivery: true", "  delivery: true\n  semantic: true"
    )
    path.write_text(text, encoding="utf-8")

    second = ExecutionProfile.create(load_project_config(config_root), "codex-host")

    assert first == second


def test_profile_binds_host_executable_and_excludes_environment(config_root: Path) -> None:
    config = load_project_config(config_root)

    first = ExecutionProfile.create(config, "codex-a")
    second = ExecutionProfile.create(config, "codex-b")

    assert first.digest != second.digest
    assert "TOKEN" not in repr(first)


def test_legacy_write_run_digest_is_unchanged_when_profile_is_none() -> None:
    inputs = WriteRunInputs(
        1,
        "prj_0123456789ABCDEFGHJKMNPQRS",
        "E001",
        None,
        "sha256:" + "1" * 64,
        "a" * 40,
        "main",
        "feat/e001",
        "sha256:" + "2" * 64,
        20,
        2,
        True,
        3600,
        True,
    )
    legacy = write_run_inputs_digest(inputs)
    explicit_none = write_run_inputs_digest(replace(inputs, execution_profile_digest=None))

    assert explicit_none == legacy


def test_profile_bound_write_run_digest_changes() -> None:
    common = dict(
        schema_version=1,
        project_id="prj_0123456789ABCDEFGHJKMNPQRS",
        scope_id="E001",
        parent_scope_id=None,
        source_revision="sha256:" + "1" * 64,
        target_baseline_head="a" * 40,
        base_branch="main",
        scope_branch="feat/e001",
        work_plan_digest="sha256:" + "2" * 64,
        max_work_items_per_run=20,
        max_repair_cycles=2,
        semantic_review_enabled=True,
        checkpoint_ttl_seconds=3600,
        delivery_review_enabled=True,
    )

    legacy = write_run_inputs_digest(WriteRunInputs(**common))
    bound = write_run_inputs_digest(
        WriteRunInputs(**common, execution_profile_digest="sha256:" + "3" * 64)
    )

    assert bound != legacy
