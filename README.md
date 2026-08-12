# Agent Graph Engine

Agent Graph Engine is an early-stage Python project for deterministic orchestration of
agent workflows. M001 provides the in-memory deterministic Graph Core. M002 adds a
local durable runtime foundation with atomic state, a checksummed journal, project
locking, and fail-closed recovery assessment. M003 adds neutral local process and Git
infrastructure with bounded output, cancellation, redacted receipts, machine-readable
repository snapshots, and explicit safe local Git mutations.
M004 adds neutral immutable WorkSource contracts and a read-only SpecKit compatibility adapter
for validating, selecting, and packaging declared repository work from content-based snapshots.
M005 adds read-only shadow integration across project identity, local Git inspection, WorkSource
selection, and the unchanged canonical graph. The engine can now inspect a real target repository,
resolve work, and reach the `EXPLORE` boundary without executing an LLM or modifying the target
repository.

The project requires Python 3.11 or newer. It has no CLI, Codex, GitHub, LLM provider,
automatic coding workflow, or real coding-task execution.

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
Each project permits one unfinished writer run; incomplete initialization remains in
preserved staging until an explicit library recovery call retries it.

`agentgraph.infra.ProcessRunner` always executes structural argv with no shell, captures
stdout and stderr through system temporary files, bounds returned bytes, and reaps the
child after timeout or cancellation. POSIX termination targets the process group. The
standard-library Windows implementation always reaps the main child but can only provide
best-effort termination of descendants because it does not use Job Objects.

`agentgraph.infra.GitAdapter` provides repository discovery and snapshots, diff inspection,
branch creation/switching, explicit staging, and local commits. It has no remote operations
and no reset, clean, merge, rebase, or other destructive workflow primitives.

`agentgraph.adapters.speckit.SpecKitAdapter` can inspect, validate, deterministically select,
and package source work. It cannot execute work, run declared checks, mutate task checkboxes or
scope status. `agentgraph.integration.ShadowRunner` can project one immutable repository/work
snapshot through deterministic `START`, `DISCOVER_PROJECT`, `PREFLIGHT`, and `SELECT_WORK` nodes.
It stops before `EXPLORE`, never executes validation commands, and does not create a durable run.
