from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.agents import EXPLORE_ANALYSIS_SCHEMA, AgentContext, AgentRequest
from agentgraph.infra import GitAdapter
from agentgraph.providers.codex import (
    CodexAgentProvider,
    CodexChangeProvider,
    CodexProviderConfig,
)
from agentgraph.write import WriteSliceOutcome, WriteSliceRequest
from agentgraph.write.evidence import read_evidence
from tests.integration.conftest import git, semantic_git_state, working_tree_bytes
from tests.integration.test_m006_vertical_slice import _target
from tests.integration.test_m008_analysis import runner


def _config() -> CodexProviderConfig:
    return CodexProviderConfig(
        executable=sys.executable,
        executable_arguments=(str(Path(__file__).with_name("_fake_codex.py")),),
        timeout_seconds=5,
    )


def _explore_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "success",
        "relevant_files": ["tracked.txt", "specs/one/tasks.md"],
        "architecture_observations": ["The task is isolated to its declared module."],
        "derived_requirements": ["Preserve the target baseline."],
        "derived_acceptance_criteria": ["The implementation is deterministic."],
        "derived_constraints": ["Do not modify source declarations."],
        "architecture_invariants": ["Write only in the external worktree."],
        "uncertainties": [],
        "reason_code": None,
        "message": None,
    }


def test_codex_agent_uses_target_as_cwd_cd_and_restricted_read_profile(
    tmp_path, monkeypatch
) -> None:
    target = _target(tmp_path)
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_CODEX_RESULT", json.dumps(_explore_result()))
    source_revision = SpecKitAdapter(SpecKitLayout(target)).snapshot().revision.fingerprint
    runtime_parent = tmp_path / "runtime"
    runtime_parent.mkdir()
    context = AgentContext(
        "prj_test",
        "run_test",
        "EXPLORE",
        "run_test:EXPLORE:1",
        target,
        runtime_parent / "attempt",
        git(target, "rev-parse", "HEAD").strip().decode(),
        source_revision,
    )
    request = AgentRequest.create(
        "explore",
        "deterministic private prompt",
        EXPLORE_ANALYSIS_SCHEMA,
        "agentgraph.explore.v1",
    )

    response = CodexAgentProvider(config=_config()).invoke(request, context)

    invocation = json.loads(capture.read_text(encoding="utf-8"))
    argv = invocation["argv"]
    assert invocation["cwd"] == str(target.resolve())
    assert argv[argv.index("--cd") + 1] == str(target.resolve())
    assert request.prompt not in " ".join(argv)
    assert invocation["prompt"] == request.prompt
    assert 'default_permissions="agentgraph_provider"' in argv
    assert 'web_search="disabled"' in argv
    assert "mcp_servers={}" in argv
    assert "--sandbox" not in argv
    assert response.input_digest == request.input_digest
    evidence = read_evidence(context.runtime_directory / "response.json")
    assert evidence["output_digest"] == response.output_digest
    assert evidence["raw_output_digest"].startswith("sha256:")
    assert (context.runtime_directory / "receipt.json").is_file()


def test_full_fake_codex_m008_path_invokes_four_roles_and_preserves_target(
    tmp_path, monkeypatch
) -> None:
    target = _target(tmp_path)
    before_git = semantic_git_state(target)
    before_bytes = working_tree_bytes(target)
    source_before = (target / "specs" / "one" / "tasks.md").read_bytes()
    count = tmp_path / "count.txt"
    monkeypatch.setenv("FAKE_CODEX_COUNT", str(count))
    monkeypatch.setenv(
        "FAKE_CODEX_RESULTS",
        json.dumps(
            {
                "explore": _explore_result(),
                "build_task_package": {
                    "schema_version": 1,
                    "status": "success",
                    "objective": "Implement T001 within its declared capability.",
                    "implementation_steps": ["Create the declared implementation module."],
                    "recommended_change_paths": ["src/t001.py"],
                    "supporting_read_paths": ["tracked.txt"],
                    "validation_focus": ["Use canonical VALIDATE after implementation."],
                    "assumptions": [],
                    "unresolved_questions": [],
                    "reason_code": None,
                    "message": None,
                },
                "assess_risk": {
                    "schema_version": 1,
                    "status": "success",
                    "risk_level": "medium",
                    "reasons": ["The change is bounded."],
                    "sensitive_areas": [],
                    "destructive_change_concerns": [],
                    "requests_human_checkpoint": False,
                    "reason_code": None,
                    "message": None,
                },
                "implement": {
                    "schema_version": 1,
                    "status": "changes",
                    "changes": [{"path": "src/t001.py", "content": "value = 42\n"}],
                    "reason_code": None,
                    "message": None,
                },
            }
        ),
    )
    config = _config()
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    report = runner(
        target,
        tmp_path / "runtime",
        CodexAgentProvider(config=config),
        CodexChangeProvider(config=config, git_adapter=adapter),
    ).run(WriteSliceRequest(scope_id="E001"))

    assert report.outcome is WriteSliceOutcome.LOCAL_COMMIT_CREATED
    assert report.executed_nodes == (
        "START",
        "DISCOVER_PROJECT",
        "PREFLIGHT",
        "SELECT_WORK",
        "EXPLORE",
        "BUILD_TASK_PACKAGE",
        "ASSESS_RISK",
        "IMPLEMENT",
        "VALIDATE",
        "REVIEW",
        "CLOSE_TASK",
        "MORE_WORK",
        "FINALIZE",
    )
    assert count.read_text(encoding="ascii") == "4"
    assert report.commit_sha is not None
    assert git(target, "rev-list", "--count", f"{report.base_head}..work/e001").strip() == b"1"
    assert semantic_git_state(target) == before_git
    assert working_tree_bytes(target) == before_bytes
    assert (target / "specs" / "one" / "tasks.md").read_bytes() == source_before
    run_path = Path(report.runtime_path or "")
    for node_id in ("EXPLORE", "BUILD_TASK_PACKAGE", "ASSESS_RISK"):
        attempts = tuple((run_path / "provider" / "codex" / "agents" / node_id).iterdir())
        assert len(attempts) == 1
        assert (attempts[0] / "receipt.json").is_file()
        assert (attempts[0] / "response.json").is_file()
        assert (attempts[0] / "analysis.json").is_file()
    assert (run_path / "provider" / "codex" / "codex-proposal.json").is_file()
