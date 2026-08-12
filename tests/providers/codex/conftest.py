from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from agentgraph.infra import GitAdapter
from agentgraph.providers.codex import CodexChangeProvider, CodexProviderConfig
from agentgraph.work import RepoPathSpec
from agentgraph.write import ChangeProviderContext, ChangeRequest
from tests.integration.conftest import git


@pytest.fixture
def codex_fixture(tmp_path: Path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.test")
    (repository / "src").mkdir()
    (repository / "src" / "existing.py").write_text("value = 1\n", encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "-m", "baseline")
    baseline = git(repository, "rev-parse", "HEAD").strip().decode()
    run_path = tmp_path / "run_test"
    provider_directory = run_path / "provider"
    provider_directory.mkdir(parents=True)
    capture = tmp_path / "capture.json"
    count = tmp_path / "count.txt"
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(capture))
    monkeypatch.setenv("FAKE_CODEX_COUNT", str(count))
    script = Path(__file__).with_name("_fake_codex.py")
    config = CodexProviderConfig(
        executable=sys.executable,
        executable_arguments=(str(script),),
        timeout_seconds=5,
        max_result_bytes=1024 * 1024,
    )
    adapter = GitAdapter(executable=shutil.which("git") or "git")
    provider = CodexChangeProvider(config=config, git_adapter=adapter)
    request = ChangeRequest(
        "prj_test",
        "T001",
        "E001",
        "Test item",
        "A goal that must travel only through stdin.",
        ("Acceptance one",),
        ("Test behavior",),
        (
            RepoPathSpec("src/existing.py", False),
            RepoPathSpec("src/new.py", False),
            RepoPathSpec("src/pkg", True),
        ),
        "sha256:source",
        baseline,
        ("keep_architecture",),
    )
    context = ChangeProviderContext(repository, provider_directory, baseline)
    return {
        "repository": repository,
        "provider": provider,
        "request": request,
        "context": context,
        "capture": capture,
        "count": count,
        "config": config,
    }


def proposal(path: str, content: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "status": "changes",
            "changes": [{"path": path, "content": content}],
            "reason_code": None,
            "message": None,
        },
        separators=(",", ":"),
    )
