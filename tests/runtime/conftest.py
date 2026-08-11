from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentgraph.core import CANONICAL_V1_GRAPH, GraphEngine, PolicySnapshot
from agentgraph.runtime import ProjectRegistry, RuntimePaths


@pytest.fixture
def fixed_now():
    return lambda: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def runtime_paths(tmp_path):
    return RuntimePaths.resolve(tmp_path / "runtime")


@pytest.fixture
def project(runtime_paths, tmp_path, fixed_now):
    target = tmp_path / "target-repo"
    target.mkdir()
    registry = ProjectRegistry(
        runtime_paths,
        project_id_factory=lambda: "prj_00000000000000000000000000",
        now=fixed_now,
    )
    return registry.register(target)


@pytest.fixture
def core_engine():
    return GraphEngine(CANONICAL_V1_GRAPH, PolicySnapshot())
