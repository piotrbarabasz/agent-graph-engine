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
M006 adds the first controlled local-write vertical slice. It can create one deterministically
reviewed local commit on a new scope branch in an external durable-run Git worktree while keeping
the target main working tree byte-identical and on its base branch.

M006 does not update the source task, push, open a pull request, merge, or invoke Codex. The
injected `ChangeProvider` returns a bounded structured text `ChangeSet`; it receives neither a
workspace path nor a writable repository capability. The project has no CLI or remote workflow.

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
branch creation/switching, external worktree creation from a pinned commit, exact local-ref
inspection, explicit staging, and local commits. It has no remote operations
and no reset, clean, merge, rebase, or other destructive workflow primitives.

`agentgraph.adapters.speckit.SpecKitAdapter` can inspect, validate, deterministically select,
and package source work. It cannot execute work, run declared checks, mutate task checkboxes or
scope status. `agentgraph.integration.ShadowRunner` can project one immutable repository/work
snapshot through deterministic `START`, `DISCOVER_PROJECT`, `PREFLIGHT`, and `SELECT_WORK` nodes.
It stops before `EXPLORE`, never executes validation commands, and does not create a durable run.

`agentgraph.write.WriteSliceRunner` reuses pinned M005 preparation and the unchanged canonical
graph, then executes exactly one eligible planned-scope item with zero repairs. Generated text
changes are preflighted against independently reconstructed path capabilities, atomically applied
under `<run>/workspace`, validated there, reviewed against exact paths and hashes, and committed
with an invocation-local identity. Operation evidence remains under `<run>/operations`.
Existing file modes are preserved by atomic content replacement, and deterministic review rejects
unexpected mode changes. Durable runs persist content-digested `write-inputs.json`; a fresh
`WriteSliceRunner.resume(run_id)` can reconstruct committed safe transitions from immutable
operation evidence. The write inputs are created and synced in the initializing run before that run
is promoted or activated. The irreversible commit boundary writes `commit-witness.json` immediately
after a commit is observed, then verifies the commit's sole parent, exact changed paths, blob hashes,
and regular-file executable modes directly from the Git object database. Any later uncertainty or
mismatch remains a fail-closed recovery case.
