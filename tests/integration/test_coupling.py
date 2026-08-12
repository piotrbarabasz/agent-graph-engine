from __future__ import annotations

from pathlib import Path

PRODUCTION_ROOTS = (Path("src/agentgraph/integration"),)


def production_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for root in PRODUCTION_ROOTS
        for path in sorted(root.glob("*.py"))
    )
    +Path("src/agentgraph/nodes/deterministic.py").read_text(encoding="utf-8")


def test_shadow_production_has_no_source_specific_or_execution_coupling() -> None:
    forbidden = (
        ".specify",
        "tasks.md",
        "milestone",
        "subprocess",
        "shell=true",
        "codex",
        "openai",
        "github",
        "processrunner",
        "durablegraphcoordinator",
        "start_run(",
        ".write_text(",
        ".write_bytes(",
        ".create_branch(",
        ".switch_branch(",
        ".stage_paths(",
        ".commit(",
    )
    lowered = production_text().casefold()

    assert all(token not in lowered for token in forbidden)


def test_core_runtime_and_infra_do_not_import_shadow_integration() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (
            Path("src/agentgraph/core"),
            Path("src/agentgraph/runtime"),
            Path("src/agentgraph/infra"),
        )
        for path in sorted(root.glob("*.py"))
    )

    assert "agentgraph.integration" not in text
    assert "agentgraph.nodes" not in text
