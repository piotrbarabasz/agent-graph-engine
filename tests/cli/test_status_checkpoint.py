from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgraph.agents import DeclaredWorkAgentProvider
from agentgraph.cli import ProviderOverrides, build_application
from agentgraph.cli.errors import CliError
from agentgraph.cli.models import CliResultV1
from agentgraph.cli.output import from_status, render_human, render_json
from agentgraph.core import CheckpointOutcome
from agentgraph.runtime.codec import canonical_json_bytes, sha256_digest
from agentgraph.write import WriteSliceOutcome
from tests.integration.conftest import git
from tests.integration.test_m006_vertical_slice import NewFileProvider, _target
from tests.integration.test_m013_delivery_review import DeliveryReviewer


class CountingProvider(NewFileProvider):
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, request, context):
        self.calls += 1
        return super().propose(request, context)


class FilesystemDeclaredProvider(DeclaredWorkAgentProvider):
    def invoke(self, request, context):
        context.runtime_directory.mkdir(parents=True, exist_ok=True)
        return super().invoke(request, context)


def _configured_target(tmp_path: Path, config_text: str, *, critical: bool = False) -> Path:
    target = _target(tmp_path)
    config = config_text.replace(
        "  semantic: true\n  delivery: true", "  semantic: false\n  delivery: true"
    )
    (target / ".agentgraph.yml").write_text(config, encoding="utf-8")
    if critical:
        tasks = target / "specs" / "one" / "tasks.md"
        tasks.write_text(
            tasks.read_text(encoding="utf-8").replace("Risk: medium", "Risk: critical"),
            encoding="utf-8",
        )
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "-m", "agentgraph config")
    return target


def _application(target: Path, runtime: Path, provider: CountingProvider):
    return build_application(
        target,
        runtime_home=runtime,
        provider_overrides=ProviderOverrides(
            change_provider=provider,
            general_agent_provider=FilesystemDeclaredProvider(),
            delivery_review_provider=DeliveryReviewer(),
        ),
    )


def test_status_is_repeatable_read_only_and_profile_aware(
    tmp_path: Path, tmp_path_factory, config_text: str
) -> None:
    target = _configured_target(tmp_path, config_text)
    runtime = tmp_path_factory.mktemp("r")
    provider = CountingProvider()
    app = _application(target, runtime, provider)
    run = app.run("E001", None)
    assert run.outcome is WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED
    assert run.run_id is not None
    run_path = Path(run.runtime_path or "")
    state_before = (run_path / "state.json").read_bytes()
    journal_before = (run_path / "journal.jsonl").read_bytes()
    calls_before = provider.calls

    first = app.status(run.run_id)
    second = app.status(run.run_id)

    assert first == second
    assert first.profile_bound
    assert first.profile_match is True
    assert first.commit_sha == run.commit_sha
    assert provider.calls == calls_before
    assert (run_path / "state.json").read_bytes() == state_before
    assert (run_path / "journal.jsonl").read_bytes() == journal_before

    config_path = target / ".agentgraph.yml"
    config_path.write_text(
        "# harmless comment\n" + config_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert _application(target, runtime, provider).status(run.run_id).profile_match is True

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "max_repair_cycles: 2", "max_repair_cycles: 1"
        ),
        encoding="utf-8",
    )
    drifted = _application(target, runtime, provider)
    assert drifted.status(run.run_id).profile_match is False
    resumed = drifted.resume(run.run_id)
    assert resumed.outcome is WriteSliceOutcome.RECOVERY_REQUIRED
    assert resumed.issues[0].code == "execution_profile_mismatch"
    assert provider.calls == calls_before


def test_checkpoint_show_and_approve_do_not_resume_or_disclose_nonce(
    tmp_path: Path, tmp_path_factory, config_text: str
) -> None:
    target = _configured_target(tmp_path, config_text, critical=True)
    runtime = tmp_path_factory.mktemp("r")
    provider = CountingProvider()
    app = _application(target, runtime, provider)
    run = app.run("E001", None)
    assert run.outcome is WriteSliceOutcome.CHECKPOINT_REQUIRED
    assert run.run_id is not None
    assert run.checkpoint is not None
    nonce = run.checkpoint.nonce
    run_path = Path(run.runtime_path or "")
    state_before = (run_path / "state.json").read_bytes()
    journal_before = (run_path / "journal.jsonl").read_bytes()

    selected, shown = app.show_checkpoint(run.run_id)
    status = app.status(run.run_id)
    assert selected == run.run_id
    assert shown == status.checkpoint
    assert shown.item_id == "T001"
    assert nonce not in render_json(from_status("status", status))
    assert nonce not in render_human(from_status("status", status))
    assert "Item: T001" in render_human(from_status("status", status))
    shown_result = CliResultV1(
        command="checkpoint show",
        ok=True,
        outcome="CHECKPOINT_PENDING",
        run_id=run.run_id,
        checkpoint=shown,
    )
    assert nonce not in render_json(shown_result)
    assert nonce not in render_human(shown_result)
    assert (run_path / "state.json").read_bytes() == state_before
    assert (run_path / "journal.jsonl").read_bytes() == journal_before

    selected, decision_checkpoint = app.submit_checkpoint(
        run.run_id, outcome=CheckpointOutcome.APPROVED, actor="Piotr"
    )
    decision_result = CliResultV1(
        command="checkpoint approve",
        ok=True,
        outcome="CHECKPOINT_DECISION_RECORDED",
        run_id=selected,
        checkpoint=decision_checkpoint,
        decision="APPROVED",
        actor="Piotr",
    )
    assert nonce not in render_json(decision_result)
    assert nonce not in render_human(decision_result)

    assert provider.calls == 0
    assert (run_path / "state.json").read_bytes() == state_before
    assert (run_path / "journal.jsonl").read_bytes() == journal_before
    resumed = app.resume(run.run_id)
    assert resumed.outcome is WriteSliceOutcome.PUBLISH_PREPARATION_BLOCKED
    assert provider.calls == 1


def test_checkpoint_show_rejects_self_consistent_current_state_tamper(
    tmp_path: Path, tmp_path_factory, config_text: str
) -> None:
    target = _configured_target(tmp_path, config_text, critical=True)
    runtime = tmp_path_factory.mktemp("r")
    app = _application(target, runtime, CountingProvider())
    run = app.run("E001", None)
    assert run.run_id is not None and run.checkpoint is not None
    nonce = run.checkpoint.nonce
    path = (
        Path(run.runtime_path or "") / "checkpoints" / run.checkpoint.checkpoint_id / "request.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["state_version"] += 1
    document["request_digest"] = sha256_digest(
        {key: value for key, value in document.items() if key != "request_digest"}
    )
    path.write_bytes(canonical_json_bytes(document))

    with pytest.raises(CliError) as error:
        app.show_checkpoint(run.run_id)

    assert error.value.code == "checkpoint_binding_mismatch"
    assert nonce not in str(error.value)
