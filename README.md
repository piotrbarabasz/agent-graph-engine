# Agent Graph Engine

Agent Graph Engine is an early-stage Python project for deterministic orchestration of
agent workflows. M001 implements only the in-memory Graph Core: immutable state,
typed node results and patches, centrally enforced ownership, graph validation, and
deterministic transition selection.

The project requires Python 3.11 or newer. It currently has no CLI, persistence,
LLM, Git, or remote-provider integration.

## Development

```console
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

The package uses a `src` layout. `agentgraph.core` contains the domain models,
patch executor, graph definition, canonical v1 graph, and synchronous in-memory
engine. Runtime integrations will be introduced only in later milestones.

