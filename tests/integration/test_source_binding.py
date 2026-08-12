from __future__ import annotations

import hashlib

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.integration import (
    ShadowOutcome,
    ShadowRequest,
    WorkSourceRepositoryMismatchError,
    verify_work_source_revision,
)
from agentgraph.work import SourceDocumentRevision, WorkSourceRevision

from .conftest import (
    git,
    initialize_target,
    make_runner,
    semantic_git_state,
    working_tree_bytes,
)


def test_work_source_documents_are_bound_to_the_configured_repository(tmp_path) -> None:
    repository_a = tmp_path / "repository-a"
    repository_b = tmp_path / "repository-b"
    initialize_target(repository_a)
    initialize_target(repository_b)
    tasks_b = repository_b / "specs" / "one" / "tasks.md"
    tasks_b.write_text(
        tasks_b.read_text(encoding="utf-8").replace("Work item T001", "Different work item T001"),
        encoding="utf-8",
    )
    git(repository_b, "add", "--all")
    git(repository_b, "commit", "--quiet", "-m", "different source")
    before_a = (semantic_git_state(repository_a), working_tree_bytes(repository_a))
    before_b = (semantic_git_state(repository_b), working_tree_bytes(repository_b))
    source_b = SpecKitAdapter(SpecKitLayout(repository_b))

    report = make_runner(
        repository_a,
        tmp_path / "runtime",
        work_source=source_b,
    ).run(ShadowRequest(scope_id="E001"))

    assert report.outcome is ShadowOutcome.INVALID_SOURCE
    assert report.graph_state is None
    assert report.issues[0].code == "work_source_repository_mismatch"
    assert (semantic_git_state(repository_a), working_tree_bytes(repository_a)) == before_a
    assert (semantic_git_state(repository_b), working_tree_bytes(repository_b)) == before_b


def test_work_source_documents_matching_the_target_pass_binding(target, tmp_path) -> None:
    source = SpecKitAdapter(SpecKitLayout(target))
    snapshot = source.snapshot()

    verify_work_source_revision(target, snapshot.revision)
    report = make_runner(target, tmp_path / "runtime", work_source=source).run(
        ShadowRequest(scope_id="E001")
    )

    assert report.outcome is ShadowOutcome.READY_FOR_EXPLORE


@pytest.mark.parametrize(
    "unsafe",
    ["../outside.yml", r"dir\source.yml", "/absolute.yml", r"C:\source.yml"],
)
def test_revision_document_paths_must_be_safe_repository_relative_posix(
    target, unsafe: str
) -> None:
    revision = WorkSourceRevision(
        (SourceDocumentRevision(unsafe, "0" * 64, 0),),
        "sha256:" + "0" * 64,
    )

    with pytest.raises(WorkSourceRepositoryMismatchError):
        verify_work_source_revision(target, revision)


def test_revision_document_must_exist_and_match_size_and_digest(target) -> None:
    path = target / "tracked.txt"
    raw = path.read_bytes()
    valid = SourceDocumentRevision(
        "tracked.txt", f"sha256:{hashlib.sha256(raw).hexdigest()}", len(raw)
    )
    verify_work_source_revision(
        target,
        WorkSourceRevision((valid,), "sha256:" + "0" * 64),
    )

    invalid_documents = (
        SourceDocumentRevision("missing.txt", f"sha256:{hashlib.sha256(b'').hexdigest()}", 0),
        SourceDocumentRevision("tracked.txt", valid.sha256, len(raw) + 1),
        SourceDocumentRevision("tracked.txt", "0" * 64, len(raw)),
    )
    for document in invalid_documents:
        with pytest.raises(WorkSourceRepositoryMismatchError):
            verify_work_source_revision(
                target,
                WorkSourceRevision((document,), "sha256:" + "0" * 64),
            )
