from pathlib import Path

import pytest

from agentgraph.runtime.errors import InvalidRuntimeIdentifierError
from agentgraph.runtime.ids import (
    generate_project_id,
    generate_record_id,
    generate_run_id,
    validate_project_id,
    validate_record_id,
    validate_run_id,
)
from agentgraph.runtime.paths import RuntimePaths

MALICIOUS_RUN_IDS = (
    "run_../../outside",
    r"run_..\..\outside",
    "run_/absolute",
    r"run_\absolute",
    "run_:evil",
    "run_ bad",
)
MALICIOUS_PROJECT_IDS = tuple(value.replace("run_", "prj_", 1) for value in MALICIOUS_RUN_IDS)


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


def test_generated_runtime_ids_pass_the_canonical_validators() -> None:
    assert validate_project_id(generate_project_id()).startswith("prj_")
    assert validate_run_id(generate_run_id()).startswith("run_")
    assert validate_record_id(generate_record_id()).startswith("rec_")


@pytest.mark.parametrize(
    ("validator", "identifier"),
    [
        *((validate_run_id, value) for value in MALICIOUS_RUN_IDS),
        *((validate_project_id, value) for value in MALICIOUS_PROJECT_IDS),
        (validate_record_id, "rec_../../outside"),
        (validate_record_id, "rec_:evil"),
        (validate_run_id, "run_" + chr(0xE9) + "vil"),
        (validate_project_id, "prj_control\x00"),
        (validate_run_id, "run_" + "a" * 125),
    ],
)
def test_runtime_identifier_validators_reject_unsafe_values(validator, identifier) -> None:
    with pytest.raises(InvalidRuntimeIdentifierError):
        validator(identifier)


@pytest.mark.parametrize("project_id", MALICIOUS_PROJECT_IDS)
def test_project_path_rejects_traversal_without_filesystem_mutation(tmp_path, project_id) -> None:
    paths = RuntimePaths.resolve(tmp_path / "runtime")
    before = tuple(tmp_path.rglob("*"))

    with pytest.raises(InvalidRuntimeIdentifierError):
        paths.project(project_id)

    assert tuple(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("run_id", MALICIOUS_RUN_IDS)
@pytest.mark.parametrize("helper", ["run", "initializing_run"])
def test_run_paths_reject_traversal_without_filesystem_mutation(tmp_path, run_id, helper) -> None:
    paths = RuntimePaths.resolve(tmp_path / "runtime")
    before = tuple(tmp_path.rglob("*"))

    with pytest.raises(InvalidRuntimeIdentifierError):
        getattr(paths, helper)("prj_safe", run_id)

    assert tuple(tmp_path.rglob("*")) == before


def test_default_runtime_root_is_outside_an_arbitrary_target(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTGRAPH_HOME", raising=False)
    target = tmp_path / "target"
    assert RuntimePaths.resolve().root == Path.home().resolve() / ".agentgraph"
    assert RuntimePaths.resolve().root != target
