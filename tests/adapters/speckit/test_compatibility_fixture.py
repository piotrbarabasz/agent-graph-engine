from pathlib import Path

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.work import SelectionKind, ValidationOrigin, WorkItemStatus, WorkRisk, WorkSource


def test_compatibility_fixture_maps_to_neutral_snapshot_and_package(speckit_source) -> None:
    _, adapter = speckit_source
    assert isinstance(adapter, WorkSource)
    assert adapter.validate().ok

    snapshot = adapter.snapshot()
    selection = adapter.next_ready_item(snapshot, "E007")
    package = adapter.build_package(snapshot, "T049")

    assert selection.kind is SelectionKind.READY
    assert selection.item_id == "T049"
    assert adapter.get_item(snapshot, "T048").status is WorkItemStatus.COMPLETED
    assert package.title == "Add a provider-neutral result model"
    assert package.risk is WorkRisk.MEDIUM
    assert package.dependencies == ("T048",)
    assert package.source_location.path == "specs/001-ai-content-studio/tasks.md"
    assert package.source_location.line is not None
    assert package.source_revision is snapshot.revision
    assert len(package.item_validation_checks) == 2
    assert all(check.origin is ValidationOrigin.ITEM for check in package.item_validation_checks)
    assert len(package.scope_required_checks) == 2
    assert all(check.origin is ValidationOrigin.SCOPE for check in package.scope_required_checks)
    assert package.allowed_paths == (
        package.implementation_paths[0],
        package.implementation_paths[1],
        package.test_paths[0],
    )
    assert package.implementation_paths[1].directory_hint is True
    assert package.implementation_paths[0].path == "src/t049.py"
    assert not (Path(package.implementation_paths[0].path)).is_absolute()


def test_exact_legacy_task_format_supports_markdown_commands_and_annotated_paths(
    tmp_path,
) -> None:
    root = tmp_path / "target"
    workstreams = root / ".specify" / "workstreams"
    feature = root / "specs" / "001-legacy-format"
    workstreams.mkdir(parents=True)
    feature.mkdir(parents=True)
    (workstreams / "M001.yml").write_text(
        """\
id: M001
title: Repository foundation
status: active
goal: Establish the repository foundation.
epics:
  - E001
completion_criteria:
  - Repository checks pass.
""",
        encoding="utf-8",
    )
    (workstreams / "E001.yml").write_text(
        """\
id: E001
title: Repository scaffold
milestone: M001
feature: specs/001-legacy-format
base_branch: master
branch: epic/E001-repository-scaffold
status: planned
risk: medium
depends_on: []
tasks:
  - T001
  - T002
required_checks:
  - git diff --check
pr_policy:
  one_pr_per_epic: true
  merge_requires_human: true
  auto_merge: false
commit_policy:
  one_commit_per_task: true
  commit_requires_human: true
  auto_commit: false
        """,
        encoding="utf-8",
    )
    conditional_declaration = (
        "`none` unless a minimal test seam correction is required in "
        "`backend/app/providers/chatterbox_v3.py`"
    )
    (feature / "tasks.md").write_text(
        f"""\
- [X] T001 Repository scaffold
Milestone: M001
Epic: E001
Risk: medium
Implementation files: `README.md`, `backend/`
Test files: `none` (documentation or configuration validation is covered by repository checks)
Validation commands: `git diff --check`
Final PR review required: yes
Goal: Establish the repository scaffold.
Dependencies: None
Acceptance criteria: The repository structure is documented and deterministic.
Test requirements: None.
Parallelizable: no
Notes: Repository-level checks cover this documentation task.

- [ ] T002 Conditional provider correction
Milestone: M001
Epic: E001
Risk: medium
Implementation files: {conditional_declaration}
Test files: `backend/tests/unit/test_t054.py`
Validation commands: `python -m pytest backend/tests/unit/test_t054.py`
Final PR review required: yes
Goal: Verify the provider seam without speculative edits.
Dependencies: T001
Acceptance criteria: The focused provider contract remains stable.
Test requirements: Run the focused unit test.
Parallelizable: no
Notes: The implementation path is conditional but explicitly bounded.
""",
        encoding="utf-8",
    )
    adapter = SpecKitAdapter(SpecKitLayout(root))

    assert adapter.validate().ok
    snapshot = adapter.snapshot()
    documentation = adapter.build_package(snapshot, "T001")
    conditional = adapter.build_package(snapshot, "T002")

    assert documentation.item_validation_checks[0].argv == ("git", "diff", "--check")
    assert documentation.test_paths == ()
    assert tuple(path.path for path in conditional.implementation_paths) == (
        "backend/app/providers/chatterbox_v3.py",
    )
    assert tuple(path.path for path in conditional.test_paths) == (
        "backend/tests/unit/test_t054.py",
    )
    assert tuple(path.path for path in conditional.allowed_paths) == (
        "backend/app/providers/chatterbox_v3.py",
        "backend/tests/unit/test_t054.py",
    )
    assert conditional.item_validation_checks[0].argv == (
        "python",
        "-m",
        "pytest",
        "backend/tests/unit/test_t054.py",
    )
