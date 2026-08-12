from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitLayout
from agentgraph.adapters.speckit.paths import parse_repo_path_list, parse_repo_path_spec
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


@pytest.mark.parametrize("declared", ["none", "`none`", "None", "N/A", "[]"])
def test_none_like_path_declarations_are_empty(tmp_path, declared: str) -> None:
    assert parse_repo_path_list(tmp_path.resolve(), declared) == ()


def test_explanatory_none_path_declaration_is_empty(tmp_path) -> None:
    declared = "`none` (documentation or configuration validation is covered by repository checks)"

    assert parse_repo_path_list(tmp_path.resolve(), declared) == ()


def test_conditional_none_extracts_only_explicit_markdown_code_paths(tmp_path) -> None:
    declared = (
        "`none` unless a minimal test seam correction is required in "
        "`backend/app/providers/chatterbox_v3.py`"
    )

    paths = parse_repo_path_list(tmp_path.resolve(), declared)

    assert tuple(path.path for path in paths) == ("backend/app/providers/chatterbox_v3.py",)


@pytest.mark.parametrize(
    "declared",
    [
        "none unless maybe backend/foo.py",
        "none something random",
        "none or perhaps files elsewhere",
        "`none` (see `backend/foo.py`)",
    ],
)
def test_ambiguous_annotated_none_path_declarations_fail_closed(tmp_path, declared: str) -> None:
    with pytest.raises(WorkSourcePathError):
        parse_repo_path_list(tmp_path.resolve(), declared)


def test_normal_markdown_path_list_behavior_is_preserved(tmp_path) -> None:
    paths = parse_repo_path_list(tmp_path.resolve(), "`a.py`, `b.py`, `dir/`")

    assert tuple(path.path for path in paths) == ("a.py", "b.py", "dir")
    assert paths[-1].directory_hint is True
