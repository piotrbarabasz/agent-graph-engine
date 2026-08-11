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
