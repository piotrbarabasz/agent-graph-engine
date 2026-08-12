from __future__ import annotations

from pathlib import Path

import pytest

from agentgraph.adapters.speckit import SpecKitAdapter, SpecKitLayout

PARENT = """\
id: M004
title: Real TTS Voiceover
status: active
goal: Deliver a provider-neutral voiceover foundation.
epics:
  - E007
completion_criteria:
  - All declared work is completed.
"""

CHILD = """\
id: E007
title: TTS Contract and Narration Fixtures
milestone: M004
feature: specs/001-ai-content-studio
base_branch: master
branch: epic/E007-tts-contract-fixtures
status: planned
risk: high
depends_on: []
tasks:
  - T048
  - T049
  - T050
required_checks:
  - "python -m pytest"
  - "git --no-pager diff --check"
pr_policy:
  one_pr_per_epic: true
  merge_requires_human: true
  auto_merge: false
commit_policy:
  one_commit_per_task: true
  commit_requires_human: true
  auto_commit: false
"""


def task_block(
    item_id: str,
    title: str,
    *,
    completed: bool = False,
    owner: str = "E007",
    parent: str = "M004",
    dependencies: str = "None",
) -> str:
    mark = "X" if completed else " "
    return f"""\
- [{mark}] {item_id} {title}
  - **Milestone:** {parent}
  - **Epic:** {owner}
  - **Risk:** medium
  - **Implementation files:** `src/{item_id.lower()}.py`, `shared/`
  - **Test files:** `tests/{item_id.lower()}_test.py`, `shared/`
  - **Validation commands:** python -m pytest tests/{item_id.lower()}_test.py; git diff --check
  - **Final PR review required:** yes
  - **Goal:** Preserve this natural-language goal; do not rewrite it.
  - **Dependencies:** {dependencies}
  - **Acceptance criteria:** The contract remains deterministic; wording is preserved.
  - **Test requirements:** Add focused unit coverage for this item.
  - **Parallelizable:** no
  - **Notes:** Compatibility fixture metadata.
"""


def write_compatibility_source(root: Path, *, multi_scope: bool = False) -> None:
    workstreams = root / ".specify" / "workstreams"
    feature = root / "specs" / "001-ai-content-studio"
    workstreams.mkdir(parents=True)
    feature.mkdir(parents=True)
    parent = PARENT
    if multi_scope:
        parent = parent.replace("  - E007\n", "  - E007\n  - E008\n")
    (workstreams / "M004.yml").write_text(parent, encoding="utf-8")
    (workstreams / "E007.yml").write_text(CHILD, encoding="utf-8")
    tasks = "# Tasks\n\n## Contract\n\n"
    tasks += task_block("T048", "Define the synthesis contract", completed=True)
    tasks += "\n" + task_block("T049", "Add a provider-neutral result model", dependencies="`T048`")
    tasks += "\n" + task_block("T050", "Add narration fixtures", dependencies="T049")
    (feature / "tasks.md").write_text(tasks, encoding="utf-8")
    (root / "marker.txt").write_text("unchanged", encoding="utf-8")
    if multi_scope:
        feature_two = root / "specs" / "002-narration"
        feature_two.mkdir(parents=True)
        (workstreams / "E008.yml").write_text(
            CHILD.replace("id: E007", "id: E008")
            .replace("TTS Contract and Narration Fixtures", "Narration Delivery")
            .replace("specs/001-ai-content-studio", "specs/002-narration")
            .replace("epic/E007-tts-contract-fixtures", "epic/E008-narration")
            .replace("depends_on: []", "depends_on:\n  - E007")
            .replace("  - T048\n  - T049\n  - T050", "  - T051"),
            encoding="utf-8",
        )
        (feature_two / "tasks.md").write_text(
            "# Tasks\n\n"
            + task_block(
                "T051",
                "Integrate completed contract work",
                owner="E008",
                dependencies="T050",
            ),
            encoding="utf-8",
        )


@pytest.fixture
def speckit_source(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    write_compatibility_source(root)
    adapter = SpecKitAdapter(SpecKitLayout(root))
    return root, adapter
