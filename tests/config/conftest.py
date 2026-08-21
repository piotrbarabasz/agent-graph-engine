from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def config_text() -> str:
    return """\
version: 1
work:
  source: speckit
  speckit:
    workstreams_dir: .specify/workstreams
    active_scope_file: .specify/runtime/active-epic
agents:
  provider: codex
  codex:
    model: null
    timeout_seconds: 900
    max_result_bytes: 4194304
review:
  semantic: true
  delivery: true
policy:
  max_repair_cycles: 2
  max_work_items_per_run: 20
  checkpoint_ttl_seconds: 3600
  validation_timeout_seconds: 120
  max_steps: 30
  commit_mode: per_work_item
publish:
  enabled: true
  provider: github
  remote: origin
  draft: true
"""


@pytest.fixture
def config_root(tmp_path: Path, config_text: str) -> Path:
    (tmp_path / ".agentgraph.yml").write_text(config_text, encoding="utf-8")
    return tmp_path
