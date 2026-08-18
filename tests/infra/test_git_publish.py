from __future__ import annotations

from tests.infra.conftest import git_command


def test_exact_push_creates_only_requested_branch_without_force_or_tags(
    git_repo, tmp_path, git_executable
) -> None:
    root, adapter, repository = git_repo
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git_command(git_executable, bare, "init", "--quiet", "--bare")
    git_command(git_executable, root, "remote", "add", "origin", str(bare))
    head = adapter.snapshot(repository).head_sha
    assert head is not None

    result = adapter.push_exact_branch(
        repository,
        remote_name="origin",
        commit_sha=head,
        remote_branch="work/exact",
    )

    assert adapter.list_remotes(repository) == ("origin",)
    assert adapter.remote_branch_sha(repository, "origin", "work/exact") == head
    assert result.receipt.argv[-4:] == (
        "push",
        "--no-verify",
        "origin",
        f"{head}:refs/heads/work/exact",
    )
    assert not any("force" in argument for argument in result.receipt.argv)
    refs = git_command(git_executable, bare, "for-each-ref", "--format=%(refname)").stdout
    assert refs.splitlines() == [b"refs/heads/work/exact"]
