from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agentgraph.infra import GitAdapter


@pytest.fixture(scope="session")
def git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("system Git is unavailable")
    return executable


def git_command(
    executable: str,
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (executable, "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=check,
        shell=False,
        env={
            **__import__("os").environ,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        },
    )


def initialize_repository(executable: str, root: Path, *, content: str = "initial\n") -> None:
    root.mkdir()
    git_command(executable, root, "init", "--quiet")
    git_command(executable, root, "config", "user.name", "Fixture User")
    git_command(executable, root, "config", "user.email", "fixture@example.test")
    (root / "tracked.txt").write_text(content, encoding="utf-8")
    git_command(executable, root, "add", "--", "tracked.txt")
    git_command(executable, root, "commit", "--quiet", "-m", "initial")


@pytest.fixture
def git_repo(tmp_path, git_executable):
    root = tmp_path / "repo"
    initialize_repository(git_executable, root)
    adapter = GitAdapter(executable=git_executable)
    return root, adapter, adapter.discover_repository(root)
