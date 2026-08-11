"""Canonical dependency-free v1 graph definition."""

from __future__ import annotations

from .edges import (
    AllCondition,
    CheckpointOutcomeCondition,
    CompletedWorkCondition,
    DeliveryReviewCondition,
    Edge,
    FailureCategoryCondition,
    PendingResumeCondition,
    RepairCapacityCondition,
    RepairClassificationCondition,
    ResultStatusCondition,
    ReviewCondition,
    RiskCondition,
    ValidationCondition,
    WorkCondition,
    WorkLimitCondition,
)
from .enums import (
    CheckpointOutcome,
    FailureCategory,
    NodeStatus,
    NodeType,
    RepairClassification,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    ValidationVerdict,
)
from .graph import GraphDefinition
from .node import NodeDefinition

V1_NODE_IDS = (
    "START",
    "DISCOVER_PROJECT",
    "PREFLIGHT",
    "SELECT_WORK",
    "EXPLORE",
    "BUILD_TASK_PACKAGE",
    "ASSESS_RISK",
    "HUMAN_CHECKPOINT",
    "IMPLEMENT",
    "VALIDATE",
    "REVIEW",
    "CLASSIFY_FAILURE",
    "PROGRAMMER_REPAIR",
    "DEBUGGER",
    "CLOSE_TASK",
    "MORE_WORK",
    "DELIVERY_REVIEW",
    "CREATE_PR",
    "FINALIZE",
    "END",
)


def _status(*statuses: NodeStatus) -> ResultStatusCondition:
    return ResultStatusCondition(frozenset(statuses))


def _all(*conditions: object) -> AllCondition:
    return AllCondition(conditions)  # type: ignore[arg-type]


def _edge(
    edge_id: str,
    source: str,
    target: str,
    condition: object,
    *,
    priority: int = 100,
    final_status: RunStatus | None = None,
    checkpoint: bool = False,
    resume_node: str | None = None,
    terminal: bool = False,
) -> Edge:
    return Edge(
        id=edge_id,
        from_node=source,
        to_node=target,
        priority=priority,
        condition=condition,  # type: ignore[arg-type]
        final_status=final_status,
        checkpoint=checkpoint,
        resume_node=resume_node,
        terminal=terminal,
    )


def _common_failure_edges(source: str) -> list[Edge]:
    return [
        _edge(
            f"{source.lower()}-cancelled",
            source,
            "FINALIZE",
            _status(NodeStatus.CANCELLED),
            final_status=RunStatus.CANCELLED,
        ),
        _edge(
            f"{source.lower()}-blocked",
            source,
            "FINALIZE",
            _status(NodeStatus.BLOCKED, NodeStatus.CHECKPOINT_REQUIRED),
            final_status=RunStatus.BLOCKED,
        ),
        _edge(
            f"{source.lower()}-failed",
            source,
            "FINALIZE",
            _status(NodeStatus.FAILED, NodeStatus.TIMED_OUT),
            final_status=RunStatus.FAILED,
            priority=10,
        ),
    ]


def canonical_v1_graph() -> GraphDefinition:
    """Build the canonical M001 graph without runtime integrations."""

    patch_paths = {
        "DISCOVER_PROJECT": ("repository.*", "project.*", "baseline.*"),
        "PREFLIGHT": ("architecture_invariants.*", "requirements.*"),
        "SELECT_WORK": ("work.*",),
        "EXPLORE": (
            "baseline.*",
            "scope.*",
            "requirements.*",
            "acceptance_criteria.*",
        ),
        "BUILD_TASK_PACKAGE": ("task_package.*",),
        "ASSESS_RISK": ("risk.level",),
        "IMPLEMENT": ("changes.*",),
        "VALIDATE": ("validation.*", "failure.*"),
        "REVIEW": (
            "review.verdict",
            "review.safe_to_close",
            "review.findings",
            "failure.*",
        ),
        "CLASSIFY_FAILURE": ("repair.classification", "repair.history"),
        "PROGRAMMER_REPAIR": ("changes.*",),
        "DEBUGGER": ("changes.*",),
        "CLOSE_TASK": ("work.item", "work.completed_items", "work.available_items"),
        "MORE_WORK": ("work.item", "work.available_items"),
        "DELIVERY_REVIEW": (
            "review.verdict",
            "review.safe_to_create_pr",
            "review.findings",
        ),
    }
    special_types = {
        "EXPLORE": NodeType.LLM_READ_ONLY,
        "BUILD_TASK_PACKAGE": NodeType.LLM_READ_ONLY,
        "ASSESS_RISK": NodeType.LLM_READ_ONLY,
        "HUMAN_CHECKPOINT": NodeType.HUMAN_CHECKPOINT,
        "IMPLEMENT": NodeType.LLM_WRITE,
        "REVIEW": NodeType.LLM_READ_ONLY,
        "CLASSIFY_FAILURE": NodeType.LLM_READ_ONLY,
        "PROGRAMMER_REPAIR": NodeType.LLM_WRITE,
        "DEBUGGER": NodeType.LLM_WRITE,
        "CLOSE_TASK": NodeType.EXTERNAL_OPERATION,
        "DELIVERY_REVIEW": NodeType.LLM_READ_ONLY,
        "CREATE_PR": NodeType.EXTERNAL_OPERATION,
    }
    nodes = tuple(
        NodeDefinition(
            node_id,
            special_types.get(node_id, NodeType.DETERMINISTIC),
            patch_paths.get(node_id, ()),
        )
        for node_id in V1_NODE_IDS
    )

    edges: list[Edge] = []
    straight = (
        ("START", "DISCOVER_PROJECT"),
        ("DISCOVER_PROJECT", "PREFLIGHT"),
        ("PREFLIGHT", "SELECT_WORK"),
        ("EXPLORE", "BUILD_TASK_PACKAGE"),
        ("BUILD_TASK_PACKAGE", "ASSESS_RISK"),
        ("IMPLEMENT", "VALIDATE"),
        ("PROGRAMMER_REPAIR", "VALIDATE"),
        ("DEBUGGER", "VALIDATE"),
        ("CLOSE_TASK", "MORE_WORK"),
    )
    for source, target in straight:
        edges.append(
            _edge(
                f"{source.lower()}-succeeded",
                source,
                target,
                _status(NodeStatus.SUCCEEDED),
            )
        )
        edges.extend(_common_failure_edges(source))

    edges.extend(
        [
            _edge(
                "select-work-found",
                "SELECT_WORK",
                "EXPLORE",
                _all(_status(NodeStatus.SUCCEEDED), WorkCondition("current", True)),
            ),
            _edge(
                "select-work-noop",
                "SELECT_WORK",
                "FINALIZE",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    WorkCondition("current", False),
                    CompletedWorkCondition(False),
                ),
                final_status=RunStatus.COMPLETED,
            ),
            _edge(
                "select-work-delivery-complete",
                "SELECT_WORK",
                "DELIVERY_REVIEW",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    WorkCondition("current", False),
                    CompletedWorkCondition(True),
                ),
            ),
        ]
    )
    edges.extend(_common_failure_edges("SELECT_WORK"))

    create_pr_operational_failures = frozenset(
        {
            FailureCategory.INFRASTRUCTURE,
            FailureCategory.ENVIRONMENT,
            FailureCategory.EXTERNAL_SERVICE,
        }
    )
    create_pr_terminal_failures = frozenset(
        {
            FailureCategory.POLICY,
            FailureCategory.CONTRACT,
            FailureCategory.INTERNAL,
            FailureCategory.IMPLEMENTATION,
            FailureCategory.DESIGN,
            FailureCategory.VALIDATION,
        }
    )
    edges.extend(
        [
            _edge(
                "create-pr-succeeded",
                "CREATE_PR",
                "FINALIZE",
                _status(NodeStatus.SUCCEEDED),
            ),
            _edge(
                "create-pr-operational-failure",
                "CREATE_PR",
                "FINALIZE",
                FailureCategoryCondition(create_pr_operational_failures),
                final_status=RunStatus.BLOCKED,
            ),
            _edge(
                "create-pr-terminal-failure",
                "CREATE_PR",
                "FINALIZE",
                FailureCategoryCondition(create_pr_terminal_failures),
                final_status=RunStatus.FAILED,
            ),
            _edge(
                "create-pr-timeout",
                "CREATE_PR",
                "FINALIZE",
                _status(NodeStatus.TIMED_OUT),
                final_status=RunStatus.BLOCKED,
            ),
            _edge(
                "create-pr-cancelled",
                "CREATE_PR",
                "FINALIZE",
                _status(NodeStatus.CANCELLED),
                final_status=RunStatus.CANCELLED,
            ),
            _edge(
                "create-pr-blocked",
                "CREATE_PR",
                "FINALIZE",
                _status(NodeStatus.BLOCKED, NodeStatus.CHECKPOINT_REQUIRED),
                final_status=RunStatus.BLOCKED,
            ),
        ]
    )

    for levels, target, checkpoint, resume in (
        ((RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH), "IMPLEMENT", False, None),
        ((RiskLevel.CRITICAL,), "HUMAN_CHECKPOINT", True, "IMPLEMENT"),
    ):
        edges.append(
            _edge(
                f"risk-{'critical' if checkpoint else 'implement'}",
                "ASSESS_RISK",
                target,
                _all(_status(NodeStatus.SUCCEEDED), RiskCondition(frozenset(levels))),
                checkpoint=checkpoint,
                resume_node=resume,
            )
        )
    edges.extend(_common_failure_edges("ASSESS_RISK"))

    for target in ("IMPLEMENT", "CREATE_PR"):
        edges.append(
            _edge(
                f"checkpoint-approved-{target.lower()}",
                "HUMAN_CHECKPOINT",
                target,
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    CheckpointOutcomeCondition(frozenset({CheckpointOutcome.APPROVED})),
                    PendingResumeCondition(target),
                ),
            )
        )
    edges.extend(
        [
            _edge(
                "checkpoint-rejected",
                "HUMAN_CHECKPOINT",
                "FINALIZE",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    CheckpointOutcomeCondition(frozenset({CheckpointOutcome.REJECTED})),
                ),
                final_status=RunStatus.BLOCKED,
            ),
            _edge(
                "checkpoint-cancelled",
                "HUMAN_CHECKPOINT",
                "FINALIZE",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    CheckpointOutcomeCondition(frozenset({CheckpointOutcome.CANCELLED})),
                ),
                final_status=RunStatus.CANCELLED,
            ),
        ]
    )
    edges.extend(_common_failure_edges("HUMAN_CHECKPOINT"))

    repairable = frozenset(
        {FailureCategory.IMPLEMENTATION, FailureCategory.DESIGN, FailureCategory.VALIDATION}
    )
    fatal = frozenset(
        {
            FailureCategory.POLICY,
            FailureCategory.CONTRACT,
            FailureCategory.INFRASTRUCTURE,
            FailureCategory.ENVIRONMENT,
            FailureCategory.EXTERNAL_SERVICE,
            FailureCategory.INTERNAL,
        }
    )
    edges.extend(
        [
            _edge(
                "validate-pass",
                "VALIDATE",
                "REVIEW",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    ValidationCondition(ValidationVerdict.PASS),
                ),
            ),
            _edge(
                "validate-reported-fail",
                "VALIDATE",
                "CLASSIFY_FAILURE",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    ValidationCondition(ValidationVerdict.FAIL),
                ),
            ),
            _edge(
                "validate-repairable-failure",
                "VALIDATE",
                "CLASSIFY_FAILURE",
                FailureCategoryCondition(repairable),
            ),
            _edge(
                "validate-fatal-failure",
                "VALIDATE",
                "FINALIZE",
                FailureCategoryCondition(fatal),
                final_status=RunStatus.FAILED,
            ),
            _edge(
                "validate-timeout",
                "VALIDATE",
                "FINALIZE",
                _status(NodeStatus.TIMED_OUT),
                final_status=RunStatus.FAILED,
            ),
            _edge(
                "validate-cancelled",
                "VALIDATE",
                "FINALIZE",
                _status(NodeStatus.CANCELLED),
                final_status=RunStatus.CANCELLED,
            ),
            _edge(
                "validate-blocked",
                "VALIDATE",
                "FINALIZE",
                _status(NodeStatus.BLOCKED, NodeStatus.CHECKPOINT_REQUIRED),
                final_status=RunStatus.BLOCKED,
            ),
        ]
    )

    edges.extend(
        [
            _edge(
                "review-pass-close",
                "REVIEW",
                "CLOSE_TASK",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    ReviewCondition(ReviewVerdict.PASS, True),
                ),
            ),
            _edge(
                "review-fail",
                "REVIEW",
                "CLASSIFY_FAILURE",
                _all(_status(NodeStatus.SUCCEEDED), ReviewCondition(ReviewVerdict.FAIL)),
            ),
            _edge(
                "review-unsafe",
                "REVIEW",
                "FINALIZE",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    ReviewCondition(ReviewVerdict.PASS, False),
                ),
                final_status=RunStatus.FAILED,
            ),
            _edge(
                "review-repairable-failure",
                "REVIEW",
                "CLASSIFY_FAILURE",
                FailureCategoryCondition(repairable),
            ),
            _edge(
                "review-fatal-failure",
                "REVIEW",
                "FINALIZE",
                FailureCategoryCondition(fatal),
                final_status=RunStatus.FAILED,
            ),
            _edge(
                "review-cancelled",
                "REVIEW",
                "FINALIZE",
                _status(NodeStatus.CANCELLED),
                final_status=RunStatus.CANCELLED,
            ),
            _edge(
                "review-timeout",
                "REVIEW",
                "FINALIZE",
                _status(NodeStatus.TIMED_OUT),
                final_status=RunStatus.FAILED,
            ),
            _edge(
                "review-blocked",
                "REVIEW",
                "FINALIZE",
                _status(NodeStatus.BLOCKED, NodeStatus.CHECKPOINT_REQUIRED),
                final_status=RunStatus.BLOCKED,
            ),
        ]
    )

    for classification, target in (
        (RepairClassification.PROGRAMMER, "PROGRAMMER_REPAIR"),
        (RepairClassification.DEBUGGER, "DEBUGGER"),
    ):
        edges.append(
            _edge(
                f"classify-{classification.value}",
                "CLASSIFY_FAILURE",
                target,
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    RepairClassificationCondition(frozenset({classification})),
                    RepairCapacityCondition(True),
                ),
            )
        )
    edges.append(
        _edge(
            "classify-limit-exhausted",
            "CLASSIFY_FAILURE",
            "FINALIZE",
            _all(_status(NodeStatus.SUCCEEDED), RepairCapacityCondition(False)),
            final_status=RunStatus.FAILED,
            priority=200,
        )
    )
    edges.extend(_common_failure_edges("CLASSIFY_FAILURE"))

    edges.extend(
        [
            _edge(
                "more-work-limit",
                "MORE_WORK",
                "FINALIZE",
                _all(_status(NodeStatus.SUCCEEDED), WorkLimitCondition(True)),
                final_status=RunStatus.PAUSED,
                priority=200,
            ),
            _edge(
                "more-work-next",
                "MORE_WORK",
                "SELECT_WORK",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    WorkLimitCondition(False),
                    WorkCondition("more", True),
                ),
            ),
            _edge(
                "more-work-empty",
                "MORE_WORK",
                "DELIVERY_REVIEW",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    WorkLimitCondition(False),
                    WorkCondition("more", False),
                ),
            ),
        ]
    )
    edges.extend(_common_failure_edges("MORE_WORK"))

    edges.extend(
        [
            _edge(
                "delivery-review-checkpoint",
                "DELIVERY_REVIEW",
                "HUMAN_CHECKPOINT",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    DeliveryReviewCondition(ReviewVerdict.PASS, True),
                ),
                checkpoint=True,
                resume_node="CREATE_PR",
            ),
            _edge(
                "delivery-review-failed",
                "DELIVERY_REVIEW",
                "FINALIZE",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    DeliveryReviewCondition(ReviewVerdict.FAIL),
                ),
                final_status=RunStatus.FAILED,
            ),
            _edge(
                "delivery-review-unsafe",
                "DELIVERY_REVIEW",
                "FINALIZE",
                _all(
                    _status(NodeStatus.SUCCEEDED),
                    DeliveryReviewCondition(ReviewVerdict.PASS, False),
                ),
                final_status=RunStatus.FAILED,
            ),
        ]
    )
    edges.extend(_common_failure_edges("DELIVERY_REVIEW"))

    edges.extend(
        [
            _edge(
                "finalize-end",
                "FINALIZE",
                "END",
                _status(NodeStatus.SUCCEEDED),
                terminal=True,
            ),
            _edge(
                "finalize-failed-end",
                "FINALIZE",
                "END",
                _status(NodeStatus.FAILED, NodeStatus.TIMED_OUT),
                final_status=RunStatus.FAILED,
                terminal=True,
            ),
            _edge(
                "finalize-cancelled-end",
                "FINALIZE",
                "END",
                _status(NodeStatus.CANCELLED),
                final_status=RunStatus.CANCELLED,
                terminal=True,
            ),
            _edge(
                "finalize-blocked-end",
                "FINALIZE",
                "END",
                _status(NodeStatus.BLOCKED, NodeStatus.CHECKPOINT_REQUIRED),
                final_status=RunStatus.BLOCKED,
                terminal=True,
            ),
        ]
    )
    return GraphDefinition(nodes, tuple(edges))


CANONICAL_V1_GRAPH = canonical_v1_graph()
