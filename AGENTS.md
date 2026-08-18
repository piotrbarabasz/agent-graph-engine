# Agent Graph Engine contributor rules

- Core behavior is deterministic and must remain testable entirely in memory.
- `agentgraph.core` has no Spec Kit knowledge and uses neutral `work_item` and
  `DeliveryScope` terminology. Source hierarchies are opaque metadata.
- Transition logic never lives in prompts. A node never selects or runs its successor.
- Retry and repair counters are engine-owned, and patch ownership is enforced centrally.
- A one-writer policy is a future execution concern, not part of M001.
- Use `DELIVERY_REVIEW`; never introduce `EPIC_REVIEW`.
- M001 has no Git, Codex, GitHub, subprocess, or persistence integration.
- Every graph behavior change requires contract and transition tests.
- Core remains independent of runtime; runtime artifacts always live outside the target repository.
- State writes are atomic, journal writes are append-only, and every state update requires CAS.
- Durable step order is fixed: started, result recorded, state CAS, transition committed.
- Recovery never guesses. Interrupted write/external nodes block without a reconciler.
- Locks use an OS advisory primitive; metadata is diagnostic and stale leases are never silently taken.
- Every durable crash boundary requires a fault-injection regression test.
- A project owns at most one unfinished writer run; initialization uses preserved staging evidence.
- Infrastructure commands are argv-only with `shell=False`; captured output is bounded and secrets
  never enter receipts or diagnostic representations.
- Timeout and cancellation must terminate and reap every started process. `GitAdapter` executes only
  through `ProcessRunner`.
- M003 Git operations are local-only: no remote/network or destructive operations. Inherited `GIT_*`
  context is removed, and explicit contained staging paths use `--literal-pathspecs` plus `--`.
- Core and runtime remain Git-independent.
- `agentgraph.work` is adapter-neutral; concrete adapters translate source-specific concepts into
  immutable snapshots. Selection uses declaration order from one validated snapshot.
- The M004 source adapter is strictly read-only and fails closed on invalid dependencies or unsafe
  paths. Validation commands are declarative argv, and source revisions are content-based.
- Work-source inspection never writes target files or creates adapter caches and metadata.
- M005 shadow integration is target-read-only and pins one immutable Git/work input snapshot per
  probe. Selection requires an explicit selector or active-scope evidence and never guesses globally.
- Shadow nodes never execute declared validation commands, invoke an LLM, or mutate the canonical
  graph. The in-memory probe stops before invoking `EXPLORE` and creates no durable graph run.
- Active scope and branch evidence must agree, and final repository/source drift fails closed.
- M006 generated writes occur only in an external durable-run Git worktree; the target main
  working tree never switches branch. `ChangeProvider` returns data and receives no write capability.
- Write-package capabilities and branch hints are independently reconciled before a durable run.
  M006 processes one item with zero automatic repairs and does not close source state.
- M006 never pushes or opens a PR. Interrupted write/external nodes fail closed, and external
  workspace plus immutable operation evidence are preserved for diagnosis.
- M006 atomic content writes preserve an existing file's mode and never introduce executable bits
  for new text files. A local commit is immediately bound to immutable witness evidence.
- M006 accepts a local commit only after reading its parents, exact path delta, blobs, and regular-file
  modes from the Git object database; any post-commit mismatch requires explicit recovery.
- Caller initialization artifacts are written and synced in the staging run after `RUN_STARTED` and
  before promotion, so every promoted M006 run already contains its immutable write inputs.
- Cross-process M006 resume reconstructs only from content-digested write inputs, GraphState, and
  mutually consistent operation evidence; it never replays an interrupted side-effect node.
- Codex is proposal-only and executes with a native runtime-only permission profile: filesystem root
  denied, minimal runtime paths readable, external runtime worktree readable, and tool network
  disabled. Its CLI working root is explicitly pinned to that worktree. It never owns filesystem
  mutation, Git mutation, validation, staging, or commit authority.
- Provider output is strict structured data; free-form output never becomes a `ChangeSet`.
  Existing-file before hashes are computed from the pinned external workspace, never by the model.
- One Codex invocation occurs per `IMPLEMENT` attempt, with no hidden retries or session resume.
  Provider repository mutation is detected before engine changes are applied.
- Interrupted Codex `IMPLEMENT` remains fail-closed under M002 recovery. Ambient external tool
  integrations must not bypass the provider isolation boundary.
- LLM read-only nodes analyze repository state but never own capabilities or transitions.
- Source work declarations remain authoritative and cannot be weakened by agent output. Agent
  recommendations may narrow write scope but never expand it.
- Effective risk is `max(source risk, agent risk)`; critical risk and explicit agent checkpoint
  requests route through the canonical `HUMAN_CHECKPOINT` node.
- Read-only agents reuse the M007 restricted Codex sandbox and are guarded by host-side target Git
  and WorkSource revision snapshots around every invocation.
- Rich agent output remains immutable attempt-scoped evidence; GraphState receives only bounded,
  validated projections plus evidence references and digests.
- Ordinary resume never re-invokes completed read-only nodes. Interrupted read-only recovery follows
  M002 rules and creates new attempt-scoped evidence instead of overwriting earlier attempts.
- Repair execution is graph-driven; no hidden retry loop exists outside canonical transitions.
- `repair.count` remains engine-owned and is incremented only on transition into a repair node.
- At most two repair cycles are supported in M009.
- `CLASSIFY_FAILURE` is read-only; `PROGRAMMER_REPAIR` and `DEBUGGER` are proposal-only
  `LLM_WRITE` nodes.
- All repairs operate in the same uncommitted external worktree and never create intermediate
  commits.
- A `WorkspaceManifest` represents the exact current effective diff against the pinned baseline.
- Validation and review evidence are cycle-aware and never reuse a previous repair-cycle result.
- Repair cannot adopt mutations introduced by validation, review, or a provider outside
  engine-owned apply.
- Final staging and commit use the final `WorkspaceManifest`, not the latest repair proposal or a
  union of touched files.
- Interrupted repair nodes remain fail-closed under M002.
- `REVIEW` remains the canonical `LLM_READ_ONLY` node; M010 adds no graph node or Core state.
- Deterministic mechanical review always runs before semantic review and cannot be overridden by an
  LLM. Final `safe_to_close` requires every enabled review layer to pass.
- Semantic review is a fresh read-only invocation over the current external worktree. It receives
  engine-bound requirements, validation evidence, and `WorkspaceManifest`, never author reasoning,
  proposals, session state, or previous semantic-review prose.
- Semantic findings may block close but never expand write capability, choose a repair transition,
  or increment repair capacity. Semantic FAIL uses the existing M009 review-failure path.
- Semantic provider contract and infrastructure failures are fatal review failures, not code-repair
  signals. Completed review evidence is cycle-aware and bound to the exact current manifest.
- Reviewer mutation of the dirty worktree is detected by the content-hash manifest guard and is
  preserved for diagnosis without automatic cleanup.
- Checkpoint transitions pause execution before `HUMAN_CHECKPOINT` until a durable human decision
  exists. Decisions are immutable, nonce-bound, actor-attributed, expiring, single-use records
  stored outside the target repository.
- Checkpoint approval binds the exact GraphState, WriteInputs/package, source revision, baseline
  commit/tree, capability fingerprint, and current operation state. It never weakens later live
  rechecks, capability checks, validation, or review.
- `HUMAN_CHECKPOINT` is safely rerunnable only because the external decision is persisted before
  node invocation and the node itself only reads and verifies immutable evidence.
- M012 executes frozen-plan work items sequentially in one external worktree and scope branch.
  Each item has fresh analysis, capability, repair capacity, evidence, and one verified commit;
  the next item starts at that commit while target main remains pinned to the run baseline.
- M012 stops before invoking `DELIVERY_REVIEW` and does not implement `CREATE_PR`, push, merge,
  remote approval, automatic continuation of limit-paused runs, or parallel item execution.
- `DELIVERY_REVIEW` reviews the exact cumulative delivery from the original target baseline to the
  final verified scope HEAD, and runs only after every planned item is independently verified.
- The delivery reviewer is a separate explicit `AgentProvider` role. It receives declared work and
  final Git authority, never implementation plans or previous semantic-review prose.
- Deterministic mechanical delivery checks precede semantic delivery review. Delivery review is
  read-only and cannot alter target, source, scope workspace, commits, or capability.
- A delivery PASS sets `review.safe_to_create_pr` but publishes nothing. M013 stops at
  `HUMAN_CHECKPOINT` with `pending_resume_node=CREATE_PR`; M014 will bind publish approval to the
  exact remote, push, and PR target.
- Delivery FAIL is terminal for the current run; no scope-wide repair loop exists in M013.
- M013 performs no push, PR creation, merge, deployment, or source closure.
- Git push is permitted only inside `CREATE_PR` after exact durable publish approval.
- Publish approval binds the repository, base/head branches, final commit/tree, draft PR content,
  delivery manifest, and delivery-review evidence.
- Publication pushes the exact final commit, never mutable `HEAD`, and never force-pushes.
- GitHub draft PR creation is idempotently reconciled through a deterministic operation marker;
  remote-service responsibilities remain separate from Git mutation.
- Interrupted `CREATE_PR` is the only external-operation node with explicit remote reconciliation.
- M014 performs no merge, deployment, or WorkSource closure.
