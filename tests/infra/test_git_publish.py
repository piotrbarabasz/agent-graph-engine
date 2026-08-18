from __future__ import annotations

from agentgraph.infra import GitRemoteEndpoint
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


def test_exact_endpoint_inspection_and_push_do_not_follow_changed_fetch_remote(
    git_repo, tmp_path, git_executable
) -> None:
    root, adapter, repository = git_repo
    fetch_bare = tmp_path / "fetch.git"
    push_bare = tmp_path / "push.git"
    fetch_bare.mkdir()
    push_bare.mkdir()
    git_command(git_executable, fetch_bare, "init", "--quiet", "--bare")
    git_command(git_executable, push_bare, "init", "--quiet", "--bare")
    git_command(git_executable, root, "remote", "add", "origin", str(fetch_bare))
    git_command(
        git_executable,
        root,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        str(push_bare),
    )
    endpoint = GitRemoteEndpoint(adapter.remote_push_urls(repository, "origin")[0])
    head = adapter.snapshot(repository).head_sha
    assert head is not None

    adapter.push_exact_branch_to_endpoint(
        repository,
        endpoint=endpoint,
        commit_sha=head,
        remote_branch="work/exact-endpoint",
    )

    assert (
        adapter.remote_branch_sha_at_endpoint(repository, endpoint, "work/exact-endpoint") == head
    )
    assert (
        git_command(git_executable, push_bare, "rev-parse", "refs/heads/work/exact-endpoint")
        .stdout.decode()
        .strip()
        == head
    )
    assert (
        git_command(
            git_executable,
            fetch_bare,
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/work/exact-endpoint",
            check=False,
        ).returncode
        == 1
    )


def test_all_push_urls_are_returned_in_git_declaration_order(
    git_repo, tmp_path, git_executable
) -> None:
    root, adapter, repository = git_repo
    first = tmp_path / "first.git"
    second = tmp_path / "second.git"
    first.mkdir()
    second.mkdir()
    git_command(git_executable, first, "init", "--quiet", "--bare")
    git_command(git_executable, second, "init", "--quiet", "--bare")
    git_command(git_executable, root, "remote", "add", "origin", str(first))
    git_command(git_executable, root, "remote", "set-url", "--add", "--push", "origin", str(first))
    git_command(
        git_executable,
        root,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        str(second),
    )

    assert adapter.remote_push_urls(repository, "origin") == (str(first), str(second))
