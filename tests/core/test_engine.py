from __future__ import annotations

from collections.abc import Iterable

import pytest

from agentgraph.core import (
    CANONICAL_V1_GRAPH,
    CheckpointOutcome,
    FailureCategory,
    GraphEngine,
    NodeStatus,
    PatchOperation,
    PolicySnapshot,
    RepairClassification,
    ReviewVerdict,
    RiskLevel,
    RunStatus,
    ValidationVerdict,
    WorkItem,
)
from tests.helpers import ResultTemplate, ScriptedNode


def template(*operations: PatchOperation) -> ResultTemplate:
    return ResultTemplate(operations=operations)


def failed(category: FailureCategory) -> ResultTemplate:
    return ResultTemplate(NodeStatus.FAILED, failure_category=category)


def base_scripts(
    *,
    risk: RiskLevel = RiskLevel.LOW,
    select_templates: Iterable[ResultTemplate] | None = None,
    validate_templates: Iterable[ResultTemplate] | None = None,
    review_templates: Iterable[ResultTemplate] | None = None,
    close_templates: Iterable[ResultTemplate] | None = None,
    classify_templates: Iterable[ResultTemplate] | None = None,
    checkpoint_templates: Iterable[ResultTemplate] | None = None,
    log: list[str] | None = None,
) -> dict[str, ScriptedNode]:
    item = WorkItem("ABC-123")
    defaults: dict[str, tuple[ResultTemplate, ...]] = {
        "START": (template(),),
        "DISCOVER_PROJECT": (template(PatchOperation.set("project.name", "demo")),),
        "PREFLIGHT": (template(),),
        "SELECT_WORK": (template(PatchOperation.set("work.item", item)),),
        "EXPLORE": (template(PatchOperation.append_unique("scope.included", "src")),),
        "BUILD_TASK_PACKAGE": (template(PatchOperation.set("task_package.ready", True)),),
        "ASSESS_RISK": (template(PatchOperation.set("risk.level", risk)),),
        "IMPLEMENT": (
            template(PatchOperation.append_unique("changes.agent_reported_files", "src/a.py")),
        ),
        "VALIDATE": (template(PatchOperation.set("validation.verdict", ValidationVerdict.PASS)),),
        "REVIEW": (
            template(
                PatchOperation.set("review.verdict", ReviewVerdict.PASS),
                PatchOperation.set("review.safe_to_close", True),
            ),
        ),
        "CLOSE_TASK": (
            template(
                PatchOperation.append_unique("work.completed_items", item),
                PatchOperation.clear("work.item"),
            ),
        ),
        "MORE_WORK": (template(),),
        "DELIVERY_REVIEW": (
            template(
                PatchOperation.set("review.verdict", ReviewVerdict.PASS),
                PatchOperation.set("review.safe_to_create_pr", True),
            ),
        ),
        "HUMAN_CHECKPOINT": (ResultTemplate(checkpoint_outcome=CheckpointOutcome.APPROVED),),
        "CREATE_PR": (template(),),
        "FINALIZE": (template(),),
        "CLASSIFY_FAILURE": (
            template(
                PatchOperation.set(
                    "repair.classification",
                    RepairClassification.PROGRAMMER,
                )
            ),
        ),
        "PROGRAMMER_REPAIR": (template(),),
        "DEBUGGER": (template(),),
    }
    overrides = {
        "SELECT_WORK": select_templates,
        "VALIDATE": validate_templates,
        "REVIEW": review_templates,
        "CLOSE_TASK": close_templates,
        "CLASSIFY_FAILURE": classify_templates,
        "HUMAN_CHECKPOINT": checkpoint_templates,
    }
    for node_id, templates in overrides.items():
        if templates is not None:
            defaults[node_id] = tuple(templates)
    return {
        node_id: ScriptedNode(node_id, *templates, call_log=log)
        for node_id, templates in defaults.items()
    }


def execute(nodes: dict[str, ScriptedNode], policy: PolicySnapshot | None = None):
    engine = GraphEngine(CANONICAL_V1_GRAPH, policy or PolicySnapshot(), nodes)
    return engine.run(engine.initial_state("full-path")), engine


def test_full_happy_path() -> None:
    log: list[str] = []
    state, _ = execute(base_scripts(log=log))
    assert state.graph.current_node == "END"
    assert state.run.status is RunStatus.COMPLETED
    assert log == [
        "START",
        "DISCOVER_PROJECT",
        "PREFLIGHT",
        "SELECT_WORK",
        "EXPLORE",
        "BUILD_TASK_PACKAGE",
        "ASSESS_RISK",
        "IMPLEMENT",
        "VALIDATE",
        "REVIEW",
        "CLOSE_TASK",
        "MORE_WORK",
        "DELIVERY_REVIEW",
        "HUMAN_CHECKPOINT",
        "CREATE_PR",
        "FINALIZE",
    ]


def test_critical_risk_checkpoint_path() -> None:
    log: list[str] = []
    checkpoints = (
        ResultTemplate(checkpoint_outcome=CheckpointOutcome.APPROVED),
        ResultTemplate(checkpoint_outcome=CheckpointOutcome.APPROVED),
    )
    state, _ = execute(
        base_scripts(
            risk=RiskLevel.CRITICAL,
            checkpoint_templates=checkpoints,
            log=log,
        )
    )
    assert state.run.status is RunStatus.COMPLETED
    assert log.count("HUMAN_CHECKPOINT") == 2
    assert log.index("HUMAN_CHECKPOINT") < log.index("IMPLEMENT")


@pytest.mark.parametrize(
    ("classification", "repair_node"),
    [
        (RepairClassification.PROGRAMMER, "PROGRAMMER_REPAIR"),
        (RepairClassification.DEBUGGER, "DEBUGGER"),
    ],
)
def test_validation_repair_then_pass_full_path(
    classification: RepairClassification, repair_node: str
) -> None:
    log: list[str] = []
    validations = (
        failed(FailureCategory.IMPLEMENTATION),
        template(PatchOperation.set("validation.verdict", ValidationVerdict.PASS)),
    )
    classifiers = (template(PatchOperation.set("repair.classification", classification)),)
    state, _ = execute(
        base_scripts(
            validate_templates=validations,
            classify_templates=classifiers,
            log=log,
        )
    )
    assert state.run.status is RunStatus.COMPLETED
    assert state.repair.count == 1
    assert repair_node in log
    assert log.count("VALIDATE") == 2


def test_review_failure_repair_review_pass_full_path() -> None:
    log: list[str] = []
    validations = (
        template(PatchOperation.set("validation.verdict", ValidationVerdict.PASS)),
        template(PatchOperation.set("validation.verdict", ValidationVerdict.PASS)),
    )
    reviews = (
        template(PatchOperation.set("review.verdict", ReviewVerdict.FAIL)),
        template(
            PatchOperation.set("review.verdict", ReviewVerdict.PASS),
            PatchOperation.set("review.safe_to_close", True),
        ),
    )
    state, _ = execute(
        base_scripts(validate_templates=validations, review_templates=reviews, log=log)
    )
    assert state.run.status is RunStatus.COMPLETED
    assert state.repair.count == 1
    assert log.count("REVIEW") == 2


def test_repair_limit_exhausted_full_path() -> None:
    log: list[str] = []
    state, _ = execute(
        base_scripts(validate_templates=(failed(FailureCategory.VALIDATION),), log=log),
        PolicySnapshot(max_repair_cycles=0),
    )
    assert state.run.status is RunStatus.FAILED
    assert state.repair.count == 0
    assert "PROGRAMMER_REPAIR" not in log


@pytest.mark.parametrize(
    ("validation", "expected_status"),
    [
        (failed(FailureCategory.INFRASTRUCTURE), RunStatus.FAILED),
        (ResultTemplate(NodeStatus.CANCELLED), RunStatus.CANCELLED),
    ],
)
def test_terminal_validation_paths(validation: ResultTemplate, expected_status: RunStatus) -> None:
    log: list[str] = []
    state, _ = execute(base_scripts(validate_templates=(validation,), log=log))
    assert state.run.status is expected_status
    assert "CLASSIFY_FAILURE" not in log
    assert "REVIEW" not in log


def test_no_work_no_op_run() -> None:
    log: list[str] = []
    state, _ = execute(base_scripts(select_templates=(template(),), log=log))
    assert state.run.status is RunStatus.COMPLETED
    for skipped_node in (
        "EXPLORE",
        "IMPLEMENT",
        "DELIVERY_REVIEW",
        "HUMAN_CHECKPOINT",
        "CREATE_PR",
    ):
        assert skipped_node not in log


def test_failing_finalize_terminates_without_loop() -> None:
    log: list[str] = []
    scripts = base_scripts(select_templates=(template(),), log=log)
    scripts["FINALIZE"] = ScriptedNode(
        "FINALIZE",
        failed(FailureCategory.INTERNAL),
        call_log=log,
    )

    state, _ = execute(scripts)

    assert state.graph.current_node == "END"
    assert state.run.status is RunStatus.FAILED
    assert log.count("FINALIZE") == 1


def test_multi_work_item_loop_then_delivery_finalize() -> None:
    first = WorkItem("A")
    second = WorkItem("B")
    log: list[str] = []
    selects = (
        template(
            PatchOperation.set("work.item", first),
            PatchOperation.set("work.available_items", (second,)),
        ),
        template(
            PatchOperation.set("work.item", second),
            PatchOperation.set("work.available_items", ()),
        ),
    )
    closes = (
        template(
            PatchOperation.append_unique("work.completed_items", first),
            PatchOperation.clear("work.item"),
        ),
        template(
            PatchOperation.append_unique("work.completed_items", second),
            PatchOperation.clear("work.item"),
        ),
    )
    validations = (
        template(PatchOperation.set("validation.verdict", ValidationVerdict.PASS)),
        template(PatchOperation.set("validation.verdict", ValidationVerdict.PASS)),
    )
    reviews = (
        template(
            PatchOperation.set("review.verdict", ReviewVerdict.PASS),
            PatchOperation.set("review.safe_to_close", True),
        ),
        template(
            PatchOperation.set("review.verdict", ReviewVerdict.PASS),
            PatchOperation.set("review.safe_to_close", True),
        ),
    )
    scripts = base_scripts(
        select_templates=selects,
        validate_templates=validations,
        review_templates=reviews,
        close_templates=closes,
        log=log,
    )
    scripts["EXPLORE"] = ScriptedNode(
        "EXPLORE",
        scripts["EXPLORE"].templates[0],
        template(),
        call_log=log,
    )
    for node_id in ("BUILD_TASK_PACKAGE", "ASSESS_RISK", "IMPLEMENT", "MORE_WORK"):
        original = scripts[node_id].templates[0]
        scripts[node_id] = ScriptedNode(node_id, original, original, call_log=log)
    state, _ = execute(scripts)
    assert state.run.status is RunStatus.COMPLETED
    assert state.work.completed_items == (first, second)
    assert log.count("SELECT_WORK") == 2
    assert log.count("MORE_WORK") == 2
    assert log.count("DELIVERY_REVIEW") == 1
