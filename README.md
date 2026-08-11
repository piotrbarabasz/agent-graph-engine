# Agent Graph Engine

Agent Graph Engine is an early-stage Python project for deterministic orchestration of
agent workflows. M001 provides the in-memory deterministic Graph Core. M002 adds a
local durable runtime foundation with atomic state, a checksummed journal, project
locking, and fail-closed recovery assessment.

The project requires Python 3.11 or newer. It has no CLI, Codex, Git, GitHub, Spec Kit,
LLM provider, or real coding-task execution.

## Development

```console
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

The package uses a `src` layout. `agentgraph.core` remains entirely in memory.
`agentgraph.runtime` stores local runtime artifacts outside the target repository,
under `~/.agentgraph` by default. Library users and tests may override this with an
explicit `RuntimePaths` root or the `AGENTGRAPH_HOME` environment variable.
