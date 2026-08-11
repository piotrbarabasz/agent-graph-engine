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

