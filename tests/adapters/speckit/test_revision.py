from __future__ import annotations


def test_revision_is_stable_and_package_reuses_snapshot_revision(speckit_source) -> None:
    _, adapter = speckit_source
    first = adapter.snapshot()
    second = adapter.snapshot()

    assert first.revision == second.revision
    assert adapter.build_package(first, "T049").source_revision is first.revision


def test_checkbox_and_manifest_comment_change_revision(speckit_source) -> None:
    root, adapter = speckit_source
    original = adapter.snapshot().revision.fingerprint
    tasks = root / "specs" / "001-ai-content-studio" / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace("- [ ] T049", "- [X] T049"), encoding="utf-8"
    )
    checkbox = adapter.snapshot().revision.fingerprint
    manifest = root / ".specify" / "workstreams" / "E007.yml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
    comment = adapter.snapshot().revision.fingerprint

    assert len({original, checkbox, comment}) == 3


def test_active_scope_and_new_relevant_manifest_change_revision(speckit_source) -> None:
    root, adapter = speckit_source
    original = adapter.snapshot().revision.fingerprint
    active = root / ".specify" / "runtime" / "active-epic"
    active.parent.mkdir(parents=True)
    active.write_text("E007\n", encoding="utf-8")
    with_active = adapter.snapshot().revision.fingerprint
    extra = root / ".specify" / "workstreams" / "M005.yml"
    extra.write_text(
        "id: M005\n"
        "title: Future\n"
        "status: completed\n"
        "goal: Future work.\n"
        "epics: []\n"
        "completion_criteria: []\n",
        encoding="utf-8",
    )
    with_manifest = adapter.snapshot().revision.fingerprint

    assert len({original, with_active, with_manifest}) == 3


def test_unrelated_file_does_not_change_revision(speckit_source) -> None:
    root, adapter = speckit_source
    original = adapter.snapshot().revision.fingerprint
    (root / "README.md").write_text("unrelated", encoding="utf-8")

    assert adapter.snapshot().revision.fingerprint == original
