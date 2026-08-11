from pathlib import Path

from agentgraph.runtime.ids import generate_project_id, generate_record_id, generate_run_id
from agentgraph.runtime.paths import RuntimePaths


def test_runtime_paths_honor_explicit_root_and_environment(tmp_path, monkeypatch) -> None:
    explicit = RuntimePaths.resolve(tmp_path / "explicit")
    assert explicit.root == (tmp_path / "explicit").resolve()
    monkeypatch.setenv("AGENTGRAPH_HOME", str(tmp_path / "environment"))
    assert RuntimePaths.resolve().root == (tmp_path / "environment").resolve()


def test_runtime_ids_are_prefixed_unique_and_filesystem_safe() -> None:
    values = [generate_project_id(), generate_project_id(), generate_run_id(), generate_record_id()]
    assert len(set(values)) == len(values)
    assert values[0].startswith("prj_")
    assert values[2].startswith("run_")
    assert values[3].startswith("rec_")
    assert all("/" not in value and "\\" not in value for value in values)


def test_default_runtime_root_is_outside_an_arbitrary_target(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTGRAPH_HOME", raising=False)
    target = tmp_path / "target"
    assert RuntimePaths.resolve().root == Path.home().resolve() / ".agentgraph"
    assert RuntimePaths.resolve().root != target
