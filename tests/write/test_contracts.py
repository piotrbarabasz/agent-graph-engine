from __future__ import annotations

import hashlib
import os
import stat

import pytest

from agentgraph.work import RepoPathSpec
from agentgraph.write import (
    ChangePathError,
    ChangeSet,
    ChangeSetError,
    FileChange,
    StaleFileError,
    apply_changeset,
    path_is_allowed,
)


def test_changeset_digest_is_deterministic_and_duplicate_paths_are_rejected() -> None:
    change = FileChange("src/a.py", None, "value = 1\n")

    assert ChangeSet.create((change,)).digest.startswith("sha256:")
    with pytest.raises(ChangeSetError, match="duplicate"):
        ChangeSet.create((change, change))


def test_directory_capability_is_component_aware() -> None:
    allowed = (RepoPathSpec("src/pkg", directory_hint=True),)

    assert path_is_allowed("src/pkg/new.py", allowed)
    assert path_is_allowed("src/pkg/x/new.py", allowed)
    assert not path_is_allowed("src/pkg2/new.py", allowed)
    assert not path_is_allowed("src/pkg", allowed)


def test_invalid_later_change_prevents_partial_apply(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "a.py"
    first.write_text("old\n", encoding="utf-8")
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    proposal = ChangeSet.create(
        (
            FileChange("a.py", digest, "new\n"),
            FileChange("outside.py", None, "bad\n"),
        )
    )

    with pytest.raises(ChangePathError, match="out_of_scope_change"):
        apply_changeset(workspace, proposal, (RepoPathSpec("a.py"),))

    assert first.read_text(encoding="utf-8") == "old\n"
    assert not (workspace / "outside.py").exists()


def test_existing_file_requires_matching_before_hash(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "a.py"
    target.write_text("old\n", encoding="utf-8")

    with pytest.raises(StaleFileError):
        apply_changeset(
            workspace,
            ChangeSet.create((FileChange("a.py", "0" * 64, "new\n"),)),
            (RepoPathSpec("a.py"),),
        )

    assert target.read_text(encoding="utf-8") == "old\n"


def test_new_text_file_is_not_created_executable(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    applied = apply_changeset(
        workspace,
        ChangeSet.create((FileChange("new.py", None, "value = 1\n"),)),
        (RepoPathSpec("new.py"),),
    )

    mode = stat.S_IMODE((workspace / "new.py").stat().st_mode)
    assert mode & 0o111 == 0
    assert applied.files[0].before_mode is None
    assert applied.files[0].after_mode == mode


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-bit semantics are unavailable")
def test_atomic_replacement_preserves_existing_executable_mode(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "run.sh"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    before = hashlib.sha256(target.read_bytes()).hexdigest()

    applied = apply_changeset(
        workspace,
        ChangeSet.create((FileChange("run.sh", before, "#!/bin/sh\necho ok\n"),)),
        (RepoPathSpec("run.sh"),),
    )

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert applied.files[0].before_mode == 0o755
    assert applied.files[0].after_mode == 0o755


def test_symlink_escape_is_rejected_when_supported(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "src"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        return  # Windows may require an unavailable developer-mode privilege.

    with pytest.raises(ChangePathError, match="symlink"):
        apply_changeset(
            workspace,
            ChangeSet.create((FileChange("src/escape.py", None, "bad\n"),)),
            (RepoPathSpec("src", directory_hint=True),),
        )

    assert not (outside / "escape.py").exists()
