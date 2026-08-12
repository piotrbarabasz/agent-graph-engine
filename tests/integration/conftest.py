from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout
from agentgraph.infra import GitAdapter
from agentgraph.integration import ShadowRunner
from agentgraph.runtime import ProjectRegistry, RuntimePaths


def git(repository: Path, *arguments: str) -> bytes:
    executable = shutil.which("git")
    assert executable is not None, "M005 integration tests require local Git"
    return subprocess.run(
        (executable, "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        shell=False,
        env={
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        },
    ).stdout


def task_block(
    item_id: str,
    *,
    owner: str,
    parent: str = "M001",
    completed: bool = False,
    dependencies: str = "None",
) -> str:
    mark = "X" if completed else " "
    return f"""\
- [{mark}] {item_id} Work item {item_id}
Milestone: {parent}
Epic: {owner}
Risk: medium
Implementation files: `src/{item_id.lower()}.py`
Test files: `tests/{item_id.lower()}.py`
Validation commands: python -c "from pathlib import Path; Path('executed.txt').write_text('bad')"
Final PR review required: yes
Goal: Inspect this item without executing it.
Dependencies: {dependencies}
Acceptance criteria: Selection and projection remain deterministic.
Test requirements: Assert the read-only boundary.
Parallelizable: no
Notes: Integration fixture item.
"""


def write_source(
    root: Path,
    *,
    status: str = "planned",
    active_scope: str | None = None,
    completed: bool = False,
    multi_scope: bool = False,
    parent_order: tuple[str, ...] = ("E001", "E002"),
    blocked_second: bool = False,
) -> None:
    workstreams = root / ".specify" / "workstreams"
    feature_one = root / "specs" / "one"
    workstreams.mkdir(parents=True)
    feature_one.mkdir(parents=True)
    child_ids = parent_order if multi_scope else ("E001",)
    children = "\n".join(f"  - {item}" for item in child_ids)
    (workstreams / "M001.yml").write_text(
        f"""\
id: M001
title: Parent scope
status: active
goal: Exercise shadow integration.
epics:
{children}
completion_criteria:
  - All work is complete.
""",
        encoding="utf-8",
    )
    (workstreams / "E001.yml").write_text(
        _child_manifest("E001", "specs/one", status, "T001"), encoding="utf-8"
    )
    (feature_one / "tasks.md").write_text(
        task_block("T001", owner="E001", completed=completed), encoding="utf-8"
    )
    if multi_scope:
        feature_two = root / "specs" / "two"
        feature_two.mkdir(parents=True)
        dependency = "E001" if blocked_second else None
        (workstreams / "E002.yml").write_text(
            _child_manifest("E002", "specs/two", "planned", "T002", dependency),
            encoding="utf-8",
        )
        (feature_two / "tasks.md").write_text(
            task_block(
                "T002",
                owner="E002",
                dependencies="T001" if blocked_second else "None",
            ),
            encoding="utf-8",
        )
    if active_scope is not None:
        active = root / ".specify" / "runtime" / "active-epic"
        active.parent.mkdir(parents=True)
        active.write_text(f"{active_scope}\n", encoding="utf-8")


def _child_manifest(
    scope_id: str,
    feature: str,
    status: str,
    item_id: str,
    dependency: str | None = None,
) -> str:
    dependencies = "[]" if dependency is None else f"\n  - {dependency}"
    return f"""\
id: {scope_id}
title: Child scope {scope_id}
milestone: M001
feature: {feature}
base_branch: main
branch: work/{scope_id.casefold()}
status: {status}
risk: medium
depends_on: {dependencies}
tasks:
  - {item_id}
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
"""


def initialize_target(root: Path, **source_options) -> None:
    root.mkdir()
    git(root, "init", "--quiet", "--initial-branch=main")
    git(root, "config", "user.name", "Fixture User")
    git(root, "config", "user.email", "fixture@example.test")
    write_source(root, **source_options)
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "initial")


def make_runner(
    root: Path,
    runtime_root: Path,
    *,
    git_adapter=None,
    work_source=None,
    id_factory: Callable[[], str] = lambda: "prj_shadow_fixture",
) -> ShadowRunner:
    adapter = git_adapter or GitAdapter(executable=shutil.which("git") or "git")
    source = work_source or SpecKitAdapter(SpecKitLayout(root))
    registry = ProjectRegistry(RuntimePaths.resolve(runtime_root), project_id_factory=id_factory)
    return ShadowRunner(
        root,
        source,
        git_adapter=adapter,
        project_registry=registry,
        run_id_factory=lambda _: "shadow_test",
    )


def semantic_git_state(root: Path) -> tuple[bytes, bytes, bytes]:
    return (
        git(root, "rev-parse", "HEAD"),
        git(root, "branch", "--show-current"),
        git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all"),
    )


def working_tree_bytes(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "target"
    initialize_target(root)
    return root
