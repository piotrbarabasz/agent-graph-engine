from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.infra.errors import GitCommandError, InvalidGitReferenceError
from tests.infra.conftest import git_command


@pytest.mark.parametrize("name", ["--evil", "foo..bar", "foo.lock", "foo bar", "foo~bar"])
def test_invalid_branch_names_fail_before_branch_mutation(git_repo, name: str) -> None:
    _, adapter, repository = git_repo
    before = adapter.snapshot(repository).branch

    with pytest.raises(InvalidGitReferenceError):
        adapter.create_branch(repository, name)

    assert adapter.snapshot(repository).branch == before


def test_read_only_operations_preserve_head_index_and_worktree(git_repo, git_executable) -> None:
    root, adapter, repository = git_repo
    (root / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    def semantics() -> tuple[bytes, bytes, bytes]:
        return (
            git_command(git_executable, root, "rev-parse", "HEAD").stdout,
            git_command(git_executable, root, "diff", "--cached", "--binary").stdout,
            git_command(git_executable, root, "status", "--porcelain=v2", "-z").stdout,
        )

    before = semantics()
    adapter.discover_repository(root)
    adapter.snapshot(repository)
    adapter.unstaged_diff_paths(repository)
    adapter.staged_diff_paths(repository)
    adapter.diff_check(repository)
    after = semantics()

    assert after == before
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "modified\n"
    assert (root / "untracked.txt").read_text(encoding="utf-8") == "untracked\n"


def test_diff_paths_and_diff_check_cover_worktree_and_index(git_repo) -> None:
    root, adapter, repository = git_repo
    tracked = root / "tracked.txt"
    tracked.write_text("bad trailing whitespace   \n", encoding="utf-8")

    assert adapter.unstaged_diff_paths(repository) == (Path("tracked.txt"),)
    working = adapter.diff_check(repository)
    assert working.working_tree_ok is False
    assert working.staged_ok is True
    assert working.ok is False

    adapter.stage_paths(repository, (tracked,))
    staged = adapter.diff_check(repository)
    assert staged.working_tree_ok is True
    assert staged.staged_ok is False
    assert adapter.staged_diff_paths(repository) == (Path("tracked.txt"),)


def test_git_command_error_exposes_only_redacted_receipt(git_repo) -> None:
    _, adapter, repository = git_repo
    with pytest.raises(GitCommandError) as raised:
        adapter.switch_branch(repository, "valid-but-missing")

    assert raised.value.receipt.command_id.startswith("cmd_")
    assert "CommandResult" not in str(raised.value)
    assert len(raised.value.receipt.stderr_preview) <= 4096
