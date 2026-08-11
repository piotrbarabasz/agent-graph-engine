from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitLayout
from agentgraph.adapters.speckit.paths import parse_repo_path_spec
from agentgraph.work import WorkSourcePathError


@pytest.mark.parametrize(
    "declared",
    [
        "../outside.py",
        r"..\outside.py",
        "/absolute/path",
        r"C:\absolute\path",
        r"\\server\share\path",
        "",
        "bad\x00path",
    ],
)
def test_unsafe_declared_paths_are_rejected(tmp_path, declared: str) -> None:
    with pytest.raises(WorkSourcePathError):
        parse_repo_path_spec(tmp_path.resolve(), declared)


def test_non_existing_paths_are_allowed_and_normalized(tmp_path) -> None:
    spec = parse_repo_path_spec(tmp_path.resolve(), "future\\module\\")

    assert spec.path == "future/module"
    assert spec.directory_hint is True


def test_existing_symlink_or_ancestor_escape_fails_closed(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        link.mkdir()
        original = Path.resolve
        outside_resolved = outside.resolve()

        def resolve(path: Path, *args, **kwargs):
            if path == link / "future.py":
                return outside_resolved / "future.py"
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)

    with pytest.raises(WorkSourcePathError):
        parse_repo_path_spec(root.resolve(), "link/future.py")


def test_layout_rejects_traversal_before_adapter_reads(tmp_path) -> None:
    root = tmp_path / "target"
    root.mkdir()
    with pytest.raises(WorkSourcePathError):
        SpecKitLayout(root, workstreams_dir="../outside")
