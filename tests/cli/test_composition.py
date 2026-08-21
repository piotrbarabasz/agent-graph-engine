from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from agentgraph.cli import build_application
from agentgraph.cli.main import main
from agentgraph.providers.codex import CodexAgentProvider


def _git(root: Path, *arguments: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run(
        (executable, "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        shell=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"},
    )


def _repository(tmp_path: Path, config_text: str) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    (root / ".agentgraph.yml").write_text(config_text, encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    return root


def test_composition_discovers_root_and_builds_distinct_roles(
    tmp_path: Path, config_text: str
) -> None:
    root = _repository(tmp_path, config_text)

    app = build_application(
        root / "nested",
        runtime_home=tmp_path / "runtime",
        codex_executable="host-codex",
    )

    assert app.repository_root == root.resolve()
    assert isinstance(app.general_agent_provider, CodexAgentProvider)
    assert isinstance(app.semantic_review_provider, CodexAgentProvider)
    assert isinstance(app.delivery_review_provider, CodexAgentProvider)
    assert (
        len(
            {
                id(app.general_agent_provider),
                id(app.semantic_review_provider),
                id(app.delivery_review_provider),
            }
        )
        == 3
    )
    assert app.runner.processes is app.process_runner
    assert app.runner.git is app.git_adapter
    assert app.profile.codex_executable_selector == "host-codex"
    assert not (tmp_path / "runtime").exists()


def test_codex_executable_precedence(tmp_path: Path, config_text: str, monkeypatch) -> None:
    root = _repository(tmp_path, config_text)
    monkeypatch.setenv("AGENTGRAPH_CODEX_EXECUTABLE", "environment-codex")

    environment = build_application(root, runtime_home=tmp_path / "runtime-a")
    explicit = build_application(
        root,
        runtime_home=tmp_path / "runtime-b",
        codex_executable="explicit-codex",
    )

    assert environment.profile.codex_executable_selector == "environment-codex"
    assert explicit.profile.codex_executable_selector == "explicit-codex"


def test_config_validate_json_is_one_document_and_creates_no_run(
    tmp_path: Path, config_text: str, capsys
) -> None:
    root = _repository(tmp_path, config_text)
    runtime = tmp_path / "runtime"

    exit_code = main(
        [
            "--repo",
            str(root / "nested"),
            "--home",
            str(runtime),
            "--json",
            "config",
            "validate",
        ]
    )
    captured = capsys.readouterr()
    document = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert document["outcome"] == "CONFIG_VALID"
    assert document["repository"] == str(root.resolve())
    assert not runtime.exists()


def test_runtime_home_inside_target_is_rejected(tmp_path: Path, config_text: str, capsys) -> None:
    root = _repository(tmp_path, config_text)

    exit_code = main(
        [
            "--repo",
            str(root),
            "--home",
            str(root / ".runtime"),
            "--json",
            "config",
            "validate",
        ]
    )
    document = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert document["error"]["code"] == "environment_invalid"
    assert not (root / ".runtime").exists()
