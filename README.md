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
M007 adds a proposal-only integration with a locally configured Codex CLI. Codex can inspect the
external worktree through a native least-privilege permission profile and return one strict
structured change proposal.
M008 adds a neutral read-only `AgentProvider` for canonical `EXPLORE`, `BUILD_TASK_PACKAGE`, and
`ASSESS_RISK`. Codex is one concrete provider. Its structured analysis is advisory: WorkSource
declarations remain authoritative, recommendations may only narrow write capability, and effective
risk is the deterministic maximum of source and agent risk.
M009 enables up to two explicit graph repair cycles. Validation or deterministic review failures
route through `CLASSIFY_FAILURE`, then `PROGRAMMER_REPAIR` or `DEBUGGER`, before returning to the
canonical `VALIDATE` and `REVIEW` nodes. Repairs remain uncommitted until one final verified local
commit.
M010 adds an optional independent semantic review layer inside the existing canonical `REVIEW`
node. Mechanical integrity review remains authoritative and runs first. When explicitly configured,
a fresh read-only `AgentProvider` evaluates the current external worktree against effective
requirements and acceptance criteria. Semantic failures use the existing M009 classification and
repair graph; reviewer contract, infrastructure, or mutation failures are fatal and never consume
repair capacity.
M011 adds a durable local human checkpoint for critical-risk work and explicit agent checkpoint
requests. Execution pauses before any implementation write and returns a nonce-bearing request with
an expiry. A separately submitted `APPROVED`, `REJECTED`, or `CANCELLED` decision is bound to the
exact pinned run state; approved runs continue through the unchanged implementation, validation,
and review pipeline.

Agent Graph Engine—not Codex—computes stale-file hashes, applies changes, runs validation, reviews
the actual diff, stages files, and verifies the local commit. The provider receives a neutral read
context but no Git mutation or engine write capability. The project does not update source tasks,
push, open pull requests, merge, or expose an AgentGraph CLI.

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
graph, then executes exactly one eligible planned-scope item with a configurable bound of zero,
one, or two graph-driven repairs. Generated text
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

M009 stores an engine-computed `WorkspaceManifest` after initial implementation and after each
successful repair. It describes the exact effective diff against the pinned baseline, including
paths, content hashes, sizes, and executable semantics, so it may shrink when a repair restores a
file. Validation and deterministic review evidence are stored per cycle. The failure classifier
reads the current dirty external worktree under a content-hash mutation guard; repair providers
remain proposal-only. Final review, staging, tree verification, and the single commit are all bound
to the latest manifest.

M010 persists whether semantic review is enabled in the immutable write inputs. A run that enabled
it cannot resume without an explicit review provider, while adding a provider to a legacy run does
not change that run's review policy. Mechanical, semantic-context, and combined final review
evidence are stored separately and per repair cycle. The semantic context binds the exact current
manifest, current-cycle validation receipts, effective requirements and acceptance criteria,
architecture invariants, baseline, source revision, and original write capability. Reviewer output
is strict bounded JSON. Finding paths are diagnostic references only and cannot expand repair write
authority. The reviewer receives neither implementation proposals nor author session/reasoning, and
each repair cycle is assessed by a fresh invocation without previous review prose.

M011 stores immutable checkpoint evidence under `<run>/checkpoints/<checkpoint-id>/`, outside the
target repository. Requests bind GraphState and operation digests, the package and write inputs,
source revision, baseline commit/tree, and write capability. The caller submits one actor-attributed
decision with the request's local capability nonce, then explicitly calls `resume(run_id)`; submit
never starts implementation. Undecided requests expire and fail closed without renewal. Decisions
created before expiry remain valid on a later resume. Approval does not bypass live drift checks,
validation, mechanical review, or optional semantic review. Remote approval, delivery review,
push, pull-request creation, and multi-item execution remain outside M011.

`agentgraph.providers.codex.CodexChangeProvider` probes the installed CLI before use, requires
non-interactive execution with an explicit Codex working root and a runtime-only permission profile.
That profile denies filesystem-root access, grants read-only access to Codex's minimal helper paths
and the external worktree, and disables tool network access. The invocation also ignores ambient
user configuration and rules and disables MCP and web-search tools. It sends the deterministic
prompt through stdin and accepts only the bounded final-result file validated against an exact
schema. Provider provenance is stored under
`<run>/provider/codex`; it contains digests, the normalized proposal, and a sanitized process receipt,
not chain-of-thought or a resumable Codex session. Any tracked, staged, untracked, conflicted, branch,
or HEAD mutation during the provider call stops `IMPLEMENT` before the engine applies a proposal.

Read-only analysis agents inspect the pinned target repository using the same restricted Codex
runtime. Every invocation is guarded by target Git and WorkSource revision snapshots before and
after the call. Rich bounded responses and sanitized receipts are immutable, attempt-scoped
evidence under `<run>/provider/<provider>/agents/<node>/<attempt>/`; GraphState stores only bounded
projections and evidence digests. Completed analysis is reconstructed from that evidence on resume,
without re-invoking completed nodes. Analysis never creates the external write worktree, executes
validation, chooses graph transitions, closes source work, or gains Git/write authority.
Malformed or contract-invalid agent output is classified as a design failure. Provider/runtime
failures and detected repository mutation are infrastructure failures; pinned baseline or source
drift and durable evidence mismatch block rather than guess.
