from pathlib import Path


def test_m006_production_has_no_forbidden_remote_or_shell_coupling() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8").casefold()
        for path in Path("src/agentgraph/write").glob("*.py")
    )
    forbidden = (
        "codex",
        "openai",
        "github",
        "shell=true",
        "os.system",
        "subprocess",
        ".write_text(",
        ".write_bytes(",
        ".switch_branch(",
        ".create_branch(",
    )

    assert all(token not in text for token in forbidden)


def test_core_and_runtime_do_not_depend_on_write_slice() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (Path("src/agentgraph/core"), Path("src/agentgraph/runtime"))
        for path in root.glob("*.py")
    )

    assert "agentgraph.write" not in text
