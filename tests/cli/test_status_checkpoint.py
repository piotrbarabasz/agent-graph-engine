from __future__ import annotations

from pathlib import Path

from agentgraph.agents import DeclaredWorkAgentProvider
from agentgraph.cli import ProviderOverrides, build_application
from agentgraph.cli.output import from_status, render_human, render_json
from agentgraph.core import CheckpointOutcome
from agentgraph.write import WriteSliceOutcome
from tests.integration.conftest import git
from tests.integration.test_m006_vertical_slice import NewFileProvider, _target


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
        "  semantic: true\n  delivery: true", "  semantic: false\n  delivery: false"
    ).replace("  enabled: true", "  enabled: false")
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
    assert run.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
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
    assert nonce not in render_json(from_status("status", status))
    assert nonce not in render_human(from_status("status", status))
    assert (run_path / "state.json").read_bytes() == state_before
    assert (run_path / "journal.jsonl").read_bytes() == journal_before

    app.submit_checkpoint(run.run_id, outcome=CheckpointOutcome.APPROVED, actor="Piotr")

    assert provider.calls == 0
    assert (run_path / "state.json").read_bytes() == state_before
    assert (run_path / "journal.jsonl").read_bytes() == journal_before
    resumed = app.resume(run.run_id)
    assert resumed.outcome is WriteSliceOutcome.DELIVERY_REVIEW_REQUIRED
    assert provider.calls == 1
