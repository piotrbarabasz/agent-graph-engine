from __future__ import annotations

import os
from pathlib import Path

from agentgraph.infra import GitCommitIdentity
from tests.infra.conftest import git_command, initialize_repository


def _repository_semantics(executable: str, root: Path) -> tuple[bytes, bytes, bytes, bytes]:
    return (
        git_command(executable, root, "rev-parse", "HEAD").stdout,
        (root / ".git" / "index").read_bytes(),
        (root / "tracked.txt").read_bytes(),
        git_command(executable, root, "status", "--porcelain=v2", "-z").stdout,
    )


def test_poisoned_git_dir_cannot_redirect_discovery_or_mutation(
    git_repo, git_executable, tmp_path, monkeypatch
) -> None:
    root_a, adapter, repository_a = git_repo
    root_b = tmp_path / "repo-b"
    initialize_repository(git_executable, root_b, content="repository B\n")
    before_b = _repository_semantics(git_executable, root_b)
    poisoned_git_dir = str(root_b / ".git")
    monkeypatch.setenv("GIT_DIR", poisoned_git_dir)

    discovered = adapter.discover_repository(root_a)
    changed = root_a / "only-a.txt"
    changed.write_text("only A", encoding="utf-8")
    adapter.stage_paths(repository_a, (changed,))

    assert discovered.root == root_a.resolve()
    assert adapter.staged_diff_paths(repository_a) == (Path("only-a.txt"),)
    assert os.environ["GIT_DIR"] == poisoned_git_dir
    monkeypatch.delenv("GIT_DIR")
    assert _repository_semantics(git_executable, root_b) == before_b


def test_poisoned_git_work_tree_cannot_redirect_snapshot_or_staging(
    git_repo, git_executable, tmp_path, monkeypatch
) -> None:
    root_a, adapter, repository_a = git_repo
    root_b = tmp_path / "repo-b"
    initialize_repository(git_executable, root_b, content="repository B\n")
    before_b = _repository_semantics(git_executable, root_b)
    poisoned_work_tree = str(root_b)
    monkeypatch.setenv("GIT_WORK_TREE", poisoned_work_tree)
    (root_a / "tracked.txt").write_text("repository A changed\n", encoding="utf-8")

    snapshot = adapter.snapshot(repository_a)
    adapter.stage_paths(repository_a, ("tracked.txt",))

    assert snapshot.unstaged_paths == (Path("tracked.txt"),)
    assert adapter.staged_diff_paths(repository_a) == (Path("tracked.txt"),)
    assert os.environ["GIT_WORK_TREE"] == poisoned_work_tree
    monkeypatch.delenv("GIT_WORK_TREE")
    assert _repository_semantics(git_executable, root_b) == before_b


def test_poisoned_git_index_file_is_not_created_or_used(git_repo, tmp_path, monkeypatch) -> None:
    root, adapter, repository = git_repo
    external_index = tmp_path / "external-index"
    poisoned_index = str(external_index)
    monkeypatch.setenv("GIT_INDEX_FILE", poisoned_index)
    changed = root / "index-safe.txt"
    changed.write_text("safe index", encoding="utf-8")

    adapter.stage_paths(repository, (changed,))

    assert adapter.staged_diff_paths(repository) == (Path("index-safe.txt"),)
    assert not external_index.exists()
    assert os.environ["GIT_INDEX_FILE"] == poisoned_index


def test_poisoned_git_identity_cannot_override_invocation_identity(
    git_repo, git_executable, monkeypatch
) -> None:
    root, adapter, repository = git_repo
    poison = {
        "GIT_AUTHOR_NAME": "Poison Author",
        "GIT_AUTHOR_EMAIL": "poison-author@example.test",
        "GIT_COMMITTER_NAME": "Poison Committer",
        "GIT_COMMITTER_EMAIL": "poison-committer@example.test",
    }
    for key, value in poison.items():
        monkeypatch.setenv(key, value)
    changed = root / "identity.txt"
    changed.write_text("identity", encoding="utf-8")
    adapter.stage_paths(repository, (changed,))

    adapter.commit(
        repository,
        "identity",
        identity=GitCommitIdentity("Expected User", "expected@example.test"),
    )

    assert {key: os.environ[key] for key in poison} == poison
    for key in poison:
        monkeypatch.delenv(key)
    metadata = git_command(
        git_executable,
        root,
        "log",
        "-1",
        "--format=%an|%ae|%cn|%ce",
    ).stdout.strip()
    assert metadata == (b"Expected User|expected@example.test|Expected User|expected@example.test")
