from __future__ import annotations

from pathlib import Path


def production_files(relative: str) -> tuple[Path, ...]:
    root = Path(__file__).parents[3]
    return tuple(sorted((root / "src" / "agentgraph" / relative).rglob("*.py")))


def test_adapter_is_read_only_process_free_and_project_neutral() -> None:
    forbidden = (
        "ai-content-generation",
        "001-ai-content-studio",
        "local_autopilot",
        "backend.app.tooling",
        "subprocess",
        "ProcessRunner",
        "GitAdapter",
        ".write_text(",
        ".write_bytes(",
        ".unlink(",
        "os.replace(",
    )
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in production_files("adapters/speckit")
    )

    assert all(token not in combined for token in forbidden)


def test_neutral_work_layer_has_no_concrete_source_vocabulary() -> None:
    forbidden = ("epic", "milestone", "speckit", ".specify", "tasks.md")
    combined = "\n".join(
        path.read_text(encoding="utf-8").casefold() for path in production_files("work")
    )

    assert all(token not in combined for token in forbidden)


def test_existing_layers_do_not_reverse_import_work_or_adapter() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for layer in ("core", "runtime", "infra")
        for path in production_files(layer)
    )

    assert "agentgraph.work" not in combined
    assert "adapters.speckit" not in combined
