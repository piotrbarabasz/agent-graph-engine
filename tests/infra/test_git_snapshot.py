from __future__ import annotations

from pathlib import Path

from agentgraph.infra.git import parse_porcelain_v2
from tests.infra.conftest import git_command


def test_clean_repository_snapshot_and_branch(git_repo) -> None:
    root, adapter, repository = git_repo
    snapshot = adapter.snapshot(repository)

    assert snapshot.root == root.resolve()
    assert snapshot.head_sha is not None and len(snapshot.head_sha) == 40
    assert snapshot.branch
    assert snapshot.detached_head is False
    assert snapshot.upstream is None
    assert snapshot.dirty is False
    assert snapshot.staged_paths == ()
    assert snapshot.unstaged_paths == ()
    assert snapshot.untracked_paths == ()


def test_snapshot_distinguishes_staged_unstaged_untracked_and_special_names(git_repo) -> None:
    root, adapter, repository = git_repo
    tracked = root / "tracked.txt"
    tracked.write_text("staged\n", encoding="utf-8")
    adapter.stage_paths(repository, (tracked,))
    tracked.write_text("staged\nunstaged\n", encoding="utf-8")
    names = ("space name.txt", "--evil.txt", "zażółć.txt")
    for name in names:
        (root / name).write_text(name, encoding="utf-8")

    snapshot = adapter.snapshot(repository)

    assert Path("tracked.txt") in snapshot.staged_paths
    assert Path("tracked.txt") in snapshot.unstaged_paths
    assert set(snapshot.untracked_paths) == {Path(name) for name in names}
    assert snapshot.dirty is True


def test_porcelain_parser_preserves_tab_without_whitespace_splitting() -> None:
    parsed = parse_porcelain_v2(b"? tab\tname.txt\0? line\nname.txt\0")

    assert set(parsed.untracked) == {Path("tab\tname.txt"), Path("line\nname.txt")}


def test_snapshot_reports_staged_rename_target(git_repo, git_executable) -> None:
    root, adapter, repository = git_repo
    git_command(git_executable, root, "mv", "tracked.txt", "renamed file.txt")

    snapshot = adapter.snapshot(repository)

    assert Path("renamed file.txt") in snapshot.staged_paths


def test_snapshot_recognizes_detached_head(git_repo, git_executable) -> None:
    root, adapter, repository = git_repo
    head = git_command(git_executable, root, "rev-parse", "HEAD").stdout.strip().decode()
    git_command(git_executable, root, "checkout", "--quiet", "--detach", head)

    snapshot = adapter.snapshot(repository)

    assert snapshot.head_sha == head
    assert snapshot.branch is None
    assert snapshot.detached_head is True


def test_unborn_repository_is_valid_snapshot(tmp_path, git_executable) -> None:
    root = tmp_path / "unborn"
    root.mkdir()
    git_command(git_executable, root, "init", "--quiet")
    adapter = __import__("agentgraph.infra", fromlist=["GitAdapter"]).GitAdapter(
        executable=git_executable
    )
    repository = adapter.discover_repository(root)

    snapshot = adapter.snapshot(repository)

    assert snapshot.head_sha is None
    assert snapshot.branch
    assert snapshot.detached_head is False


def test_snapshot_reports_conflicted_path(git_repo, git_executable) -> None:
    root, adapter, repository = git_repo
    original_branch = adapter.snapshot(repository).branch
    assert original_branch is not None
    git_command(git_executable, root, "switch", "-c", "conflict-side")
    (root / "tracked.txt").write_text("side\n", encoding="utf-8")
    git_command(git_executable, root, "commit", "-am", "side")
    git_command(git_executable, root, "switch", original_branch)
    (root / "tracked.txt").write_text("main\n", encoding="utf-8")
    git_command(git_executable, root, "commit", "-am", "main")
    merge = git_command(git_executable, root, "merge", "conflict-side", check=False)
    assert merge.returncode != 0

    snapshot = adapter.snapshot(repository)

    assert snapshot.conflicted_paths == (Path("tracked.txt"),)
    assert snapshot.dirty is True
