from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.infra import GitCommitIdentity
from agentgraph.infra.errors import GitCommandError, GitPathError, NothingToCommitError
from tests.infra.conftest import git_command


def test_create_and_switch_branch_without_force_overwrite(git_repo) -> None:
    _, adapter, repository = git_repo
    original = adapter.snapshot(repository).branch
    assert original is not None

    adapter.create_branch(repository, "feature/safe")
    assert adapter.snapshot(repository).branch == "feature/safe"
    adapter.switch_branch(repository, original)
    assert adapter.snapshot(repository).branch == original
    with pytest.raises(GitCommandError):
        adapter.create_branch(repository, "feature/safe")


def test_stage_paths_handles_option_shaped_name_and_blocks_escape(git_repo, tmp_path) -> None:
    root, adapter, repository = git_repo
    evil = root / "--evil.txt"
    evil.write_text("safe", encoding="utf-8")

    adapter.stage_paths(repository, ("--evil.txt",))

    assert adapter.staged_diff_paths(repository) == (Path("--evil.txt"),)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    for unsafe in (Path("../outside.txt"), outside, root):
        with pytest.raises(GitPathError):
            adapter.stage_paths(repository, (unsafe,))
    with pytest.raises(GitPathError):
        adapter.stage_paths(repository, "--evil.txt")


def test_stage_path_symlink_escaping_repository_fails_closed(
    git_repo, tmp_path, monkeypatch
) -> None:
    root, adapter, repository = git_repo
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        link.write_text("simulated link", encoding="utf-8")
        original_resolve = Path.resolve
        outside_resolved = outside.resolve()

        def resolve(path: Path, *args, **kwargs):
            if path == link:
                return outside_resolved
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)

    with pytest.raises(GitPathError):
        adapter.stage_paths(repository, (link,))


def test_commit_is_explicit_literal_and_identity_is_invocation_only(
    git_repo, git_executable
) -> None:
    root, adapter, repository = git_repo
    included = root / "included.txt"
    untracked = root / "untracked.txt"
    included.write_text("included", encoding="utf-8")
    untracked.write_text("not committed", encoding="utf-8")
    adapter.stage_paths(repository, (included,))
    before_name = git_command(git_executable, root, "config", "user.name").stdout
    before_email = git_command(git_executable, root, "config", "user.email").stdout
    message = 'feat: handle "quotes"; $(not-shell)'

    result = adapter.commit(
        repository,
        message,
        expected_paths=(included,),
        identity=GitCommitIdentity("Invocation User", "invocation@example.test"),
    )

    assert len(result.commit_sha) == 40
    assert (
        git_command(git_executable, root, "log", "-1", "--format=%s").stdout.strip().decode()
        == message
    )
    tree = git_command(git_executable, root, "ls-tree", "--name-only", "-r", "HEAD").stdout
    assert b"included.txt" in tree
    assert b"untracked.txt" not in tree
    assert git_command(git_executable, root, "config", "user.name").stdout == before_name
    assert git_command(git_executable, root, "config", "user.email").stdout == before_email
    assert (root / "untracked.txt").exists()


def test_commit_requires_staged_changes_and_expected_path_match(git_repo) -> None:
    root, adapter, repository = git_repo
    with pytest.raises(NothingToCommitError):
        adapter.commit(repository, "nothing")
    changed = root / "tracked.txt"
    changed.write_text("changed", encoding="utf-8")
    adapter.stage_paths(repository, (changed,))
    with pytest.raises(GitPathError, match="expected_paths"):
        adapter.commit(repository, "mismatch", expected_paths=("different.txt",))


def test_switch_failure_preserves_local_changes(git_repo, git_executable) -> None:
    root, adapter, repository = git_repo
    original = adapter.snapshot(repository).branch
    assert original is not None
    adapter.create_branch(repository, "other")
    (root / "tracked.txt").write_text("other branch\n", encoding="utf-8")
    git_command(git_executable, root, "commit", "-am", "other")
    adapter.switch_branch(repository, original)
    local = "local uncommitted\n"
    (root / "tracked.txt").write_text(local, encoding="utf-8")

    with pytest.raises(GitCommandError):
        adapter.switch_branch(repository, "other")

    assert (root / "tracked.txt").read_text(encoding="utf-8") == local
    assert adapter.snapshot(repository).branch == original
