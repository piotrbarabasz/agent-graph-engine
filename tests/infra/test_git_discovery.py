from __future__ import annotations

import pytest

from agentgraph.infra import GitAdapter
from agentgraph.infra.errors import GitUnavailableError, NotAGitRepositoryError


def test_repository_discovery_from_root_nested_directory_and_file(git_repo) -> None:
    root, adapter, repository = git_repo
    nested = root / "nested"
    nested.mkdir()
    file_path = nested / "file.txt"
    file_path.write_text("content", encoding="utf-8")

    assert repository.root == root.resolve()
    assert repository.git_dir.is_dir()
    assert adapter.discover_repository(nested) == repository
    assert adapter.discover_repository(file_path) == repository


def test_non_repository_and_missing_git_fail_with_typed_errors(tmp_path, git_executable) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(NotAGitRepositoryError):
        GitAdapter(executable=git_executable).discover_repository(outside)
    with pytest.raises(GitUnavailableError):
        GitAdapter(executable=str(tmp_path / "missing-git")).discover_repository(outside)


def test_git_adapter_exposes_no_remote_or_destructive_operations() -> None:
    forbidden = {
        "push",
        "fetch",
        "pull",
        "clone",
        "ls_remote",
        "merge",
        "rebase",
        "reset",
        "clean",
    }
    assert forbidden.isdisjoint(dir(GitAdapter))
